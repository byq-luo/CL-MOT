from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math

import torch
import torch.nn as nn
import torch.nn.functional as F
from models.decode import mot_decode
from models.losses import FocalLoss, TripletLoss, NTXentLoss
from models.losses import RegL1Loss, RegLoss, NormRegL1Loss, RegWeightedL1Loss
from models.utils import _sigmoid, _tranpose_and_gather_feat
from utils.post_process import ctdet_post_process

from .base_trainer import BaseTrainer


class MotLoss(torch.nn.Module):
    def __init__(self, opt, loss_states):
        super(MotLoss, self).__init__()
        self.opt = opt
        self.loss_states = loss_states
        self.emb_dim = opt.reid_dim
        self.nID = opt.nID

        # Loss for heatmap
        self.crit = torch.nn.MSELoss() if opt.mse_loss else FocalLoss()

        # Loss for offsets
        self.crit_reg = RegL1Loss() if opt.reg_loss == 'l1' else \
            RegLoss() if opt.reg_loss == 'sl1' else None

        # Loss for object sizes
        self.crit_wh = torch.nn.L1Loss(reduction='sum') if opt.dense_wh else \
            NormRegL1Loss() if opt.norm_wh else \
                RegWeightedL1Loss() if opt.cat_spec_wh else self.crit_reg

        # Supervised loss for object IDs
        self.IDLoss = nn.CrossEntropyLoss(ignore_index=-1)

        # FC layer for supervised object ID prediction
        self.classifier = nn.Linear(self.emb_dim, self.nID)

        # Self supervised loss for object embeddings
        self.SelfSupLoss = NTXentLoss(opt.device, 0.5) if opt.unsup_loss == 'nt_xent' else \
            TripletLoss(opt.device, 'batch_all', 0.5) if opt.unsup_loss == 'triplet_all' else \
                TripletLoss(opt.device, 'batch_hard', 0.5) if opt.unsup_loss == 'triplet_hard' else None

        if opt.unsup and self.SelfSupLoss is None:
            raise ValueError('{} is not a supported self-supervised loss. '.format(opt.unsup_loss) + \
                             'Choose nt_xent, triplet_all, or triplet_hard')

        self.emb_scale = math.sqrt(2) * math.log(self.nID - 1)
        self.s_det = nn.Parameter(-1.85 * torch.ones(1))
        self.s_id = nn.Parameter(-1.05 * torch.ones(1))

    def forward(self, output_dict, batch):
        opt = self.opt
        loss_results = {loss: 0 for loss in self.loss_states}

        outputs = output_dict['orig']
        flipped_outputs = output_dict['flipped'] if 'flipped' in output_dict else None

        # Take loss at each scale
        for s in range(opt.num_stacks):
            output = outputs[s]

            # Supervised loss on predicted heatmap
            if not opt.mse_loss:
                output['hm'] = _sigmoid(output['hm'])

            loss_results['hm'] += self.crit(output['hm'], batch['hm']) / opt.num_stacks

            # Supervised loss on object sizes
            if opt.wh_weight > 0:
                if opt.dense_wh:
                    mask_weight = batch['dense_wh_mask'].sum() + 1e-4
                    loss_results['wh'] += (self.crit_wh(output['wh'] * batch['dense_wh_mask'],
                                                        batch['dense_wh'] * batch['dense_wh_mask']) /
                                           mask_weight) / opt.num_stacks
                else:
                    loss_results['wh'] += self.crit_reg(
                        output['wh'], batch['reg_mask'],
                        batch['ind'], batch['wh']) / opt.num_stacks

            # Supervised loss on offsets
            if opt.reg_offset and opt.off_weight > 0:
                loss_results['off'] += self.crit_reg(output['reg'], batch['reg_mask'],
                                                     batch['ind'], batch['reg']) / opt.num_stacks

            id_head = _tranpose_and_gather_feat(output['id'], batch['ind'])
            id_head = id_head[batch['reg_mask'] > 0].contiguous()
            id_head = self.emb_scale * F.normalize(id_head)

            # Supervised loss on object ID predictions
            if opt.id_weight > 0 and not opt.unsup:
                id_target = batch['ids'][batch['reg_mask'] > 0]
                id_output = self.classifier(id_head).contiguous()
                loss_results['id'] += self.IDLoss(id_output, id_target)

            # Take self-supervised loss using negative sample (flipped img)
            if opt.unsup and flipped_outputs is not None:
                flipped_output = flipped_outputs[s]

                flipped_id_head = _tranpose_and_gather_feat(flipped_output['id'], batch['flipped_ind'])
                flipped_id_head = flipped_id_head[batch['reg_mask'] > 0].contiguous()
                # flipped_id_head = self.emb_scale * F.normalize(flipped_id_head)
                flipped_id_head = F.normalize(flipped_id_head)

                # Compute loss between the positive and negative set of reid features
                loss_results[opt.unsup_loss] = self.SelfSupLoss(id_head, flipped_id_head, batch['num_objs'])

        # Total supervised
        det_loss = opt.hm_weight * loss_results['hm'] + \
                   opt.wh_weight * loss_results['wh'] + \
                   opt.off_weight * loss_results['off']

        id_loss = torch.exp(-self.s_id) * loss_results['id'] if not opt.unsup else \
            torch.exp(-self.s_id) * (loss_results[opt.unsup_loss])

        # Total of supervised and self-supervised losses on object embeddings
        total_loss = torch.exp(-self.s_det) * det_loss + \
                     torch.exp(-self.s_id) * id_loss + \
                     self.s_det + self.s_id

        total_loss *= 0.5
        loss_results['loss'] = total_loss

        return total_loss, loss_results


class MotTrainer(BaseTrainer):
    def __init__(self, opt, model, optimizer=None):
        super(MotTrainer, self).__init__(opt, model, optimizer=optimizer)

    def _get_losses(self, opt):
        # We always take these losses
        loss_states = ['loss', 'hm', 'wh']

        if opt.reg_offset:
            loss_states.append('off')

        # Use either contrastive or triplet loss on object embeddings if self-supervised training
        if opt.unsup:
            loss_states.append(opt.unsup_loss)

        # Standard cross entropy loss on object IDs when supervised training
        else:
            loss_states.append('id')

        loss = MotLoss(opt, loss_states)
        return loss_states, loss

    def save_result(self, outputs, batch, results):
        output = outputs['orig'][-1]
        reg = output['reg'] if self.opt.reg_offset else None

        dets, inds = mot_decode(output['hm'], output['wh'], reg=reg,
                          cat_spec_wh=self.opt.cat_spec_wh, K=self.opt.K)

        dets = dets.detach().cpu().numpy().reshape(1, -1, dets.shape[2])

        dets_out = ctdet_post_process(dets.copy(), batch['meta']['c'].cpu().numpy(),
                                      batch['meta']['s'].cpu().numpy(),
                                      output['hm'].shape[2], output['hm'].shape[3], output['hm'].shape[1])

        results[batch['meta']['img_id'].cpu().numpy()[0]] = dets_out[0]
