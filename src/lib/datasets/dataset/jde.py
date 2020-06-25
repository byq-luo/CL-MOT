import glob
import math
import os
import os.path as osp
import random
import time
from collections import OrderedDict

import cv2
import json
import numpy as np
import torch

from torch.utils.data import Dataset
from torchvision.transforms import transforms as T
from cython_bbox import bbox_overlaps as bbox_ious
from opts import opts
from utils.image import gaussian_radius, draw_umich_gaussian, draw_msra_gaussian
from utils.utils import xyxy2xywh, generate_anchors, xywh2xyxy, encode_delta


class LoadImages:  # for inference
    def __init__(self, path, img_size=(1088, 608)):
        if os.path.isdir(path):
            image_format = ['.jpg', '.jpeg', '.png', '.tif']
            self.files = sorted(glob.glob('%s/*.*' % path))
            self.files = list(filter(lambda x: os.path.splitext(x)[1].lower() in image_format, self.files))
        elif os.path.isfile(path):
            self.files = [path]

        self.nF = len(self.files)  # number of image files
        self.width = img_size[0]
        self.height = img_size[1]
        self.count = 0

        assert self.nF > 0, 'No images found in ' + path

    def __iter__(self):
        self.count = -1
        return self

    def __next__(self):
        self.count += 1
        if self.count == self.nF:
            raise StopIteration
        img_path = self.files[self.count]

        # Read image
        img0 = cv2.imread(img_path)  # BGR
        assert img0 is not None, 'Failed to load ' + img_path

        # Padded resize
        img, _, _, _ = letterbox(img0, height=self.height, width=self.width)

        # Normalize RGB
        img = img[:, :, ::-1].transpose(2, 0, 1)
        img = np.ascontiguousarray(img, dtype=np.float32)
        img /= 255.0

        # cv2.imwrite(img_path + '.letterbox.jpg', 255 * img.transpose((1, 2, 0))[:, :, ::-1])  # save letterbox image
        return img_path, img, img0

    def __getitem__(self, idx):
        idx = idx % self.nF
        img_path = self.files[idx]

        # Read image
        img0 = cv2.imread(img_path)  # BGR
        assert img0 is not None, 'Failed to load ' + img_path

        # Padded resize
        img, _, _, _ = letterbox(img0, height=self.height, width=self.width)

        # Normalize RGB
        img = img[:, :, ::-1].transpose(2, 0, 1)
        img = np.ascontiguousarray(img, dtype=np.float32)
        img /= 255.0

        return img_path, img, img0

    def __len__(self):
        return self.nF  # number of files


class LoadVideo:  # for inference
    def __init__(self, path, img_size=(1088, 608)):
        self.cap = cv2.VideoCapture(path)
        self.frame_rate = int(round(self.cap.get(cv2.CAP_PROP_FPS)))
        self.vw = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        self.vh = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        self.vn = int(self.cap.get(cv2.CAP_PROP_FRAME_COUNT))

        self.width = img_size[0]
        self.height = img_size[1]
        self.count = 0

        self.w, self.h = 1920, 1080
        print('Lenth of the video: {:d} frames'.format(self.vn))

    def get_size(self, vw, vh, dw, dh):
        wa, ha = float(dw) / vw, float(dh) / vh
        a = min(wa, ha)
        return int(vw * a), int(vh * a)

    def __iter__(self):
        self.count = -1
        return self

    def __next__(self):
        self.count += 1
        if self.count == len(self):
            raise StopIteration
        # Read image
        res, img0 = self.cap.read()  # BGR
        assert img0 is not None, 'Failed to load frame {:d}'.format(self.count)
        img0 = cv2.resize(img0, (self.w, self.h))

        # Padded resize
        img, _, _, _ = letterbox(img0, height=self.height, width=self.width)

        # Normalize RGB
        img = img[:, :, ::-1].transpose(2, 0, 1)
        img = np.ascontiguousarray(img, dtype=np.float32)
        img /= 255.0

        # cv2.imwrite(img_path + '.letterbox.jpg', 255 * img.transpose((1, 2, 0))[:, :, ::-1])  # save letterbox image
        return self.count, img, img0

    def __len__(self):
        return self.vn  # number of files


class LoadImagesAndLabels:  # for training
    def __init__(self, path, img_size=(1088, 608), augment=False, transforms=None):
        with open(path, 'r') as file:
            self.img_files = file.readlines()
            self.img_files = [x.replace('\n', '') for x in self.img_files]
            self.img_files = list(filter(lambda x: len(x) > 0, self.img_files))

        self.label_files = [x.replace('images', 'labels_with_ids').replace('.png', '.txt').replace('.jpg', '.txt')
                            for x in self.img_files]

        self.nF = len(self.img_files)  # number of image files
        self.width = img_size[0]
        self.height = img_size[1]
        self.augment = augment
        self.transforms = transforms

    def __getitem__(self, files_index):
        img_path = self.img_files[files_index]
        label_path = self.label_files[files_index]
        return self.get_data(img_path, label_path)

    def get_data(self, img_path, label_path, unsup=False):
        height = self.height
        width = self.width

        images = {}
        labels = {}

        # Load image
        images['orig'] = cv2.imread(img_path)  # BGR
        if images['orig'] is None:
            raise ValueError('File corrupt {}'.format(img_path))

        h, w, _ = images['orig'] .shape

        # create horizontally flipped image for contrastivelearning
        if unsup:
            images['flipped'] = cv2.flip(images['orig'], 1)

        for key, img in images.items():
            # Saturation and brightness augmentation by 50%
            if self.augment:
                fraction = 0.50
                img_hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
                S = img_hsv[:, :, 1].astype(np.float32)
                V = img_hsv[:, :, 2].astype(np.float32)

                a = (random.random() * 2 - 1) * fraction + 1
                S *= a
                if a > 1:
                    np.clip(S, a_min=0, a_max=255, out=S)

                a = (random.random() * 2 - 1) * fraction + 1
                V *= a
                if a > 1:
                    np.clip(V, a_min=0, a_max=255, out=V)

                img_hsv[:, :, 1] = S.astype(np.uint8)
                img_hsv[:, :, 2] = V.astype(np.uint8)
                cv2.cvtColor(img_hsv, cv2.COLOR_HSV2BGR, dst=img)

            img, ratio, padw, padh = letterbox(img, height=height, width=width)

            # Load labels
            if os.path.isfile(label_path):
                labels_ = np.loadtxt(label_path, dtype=np.float32).reshape(-1, 6)

                if key == 'flipped' and len(labels_) > 0:
                    labels_[:, 2] = 1 - labels_[:, 2]

                # Normalized xywh to pixel xyxy format
                labels[key] = labels_.copy()
                labels[key][:, 2] = ratio * w * (labels_[:, 2] - labels_[:, 4] / 2) + padw
                labels[key][:, 3] = ratio * h * (labels_[:, 3] - labels_[:, 5] / 2) + padh
                labels[key][:, 4] = ratio * w * (labels_[:, 2] + labels_[:, 4] / 2) + padw
                labels[key][:, 5] = ratio * h * (labels_[:, 3] + labels_[:, 5] / 2) + padh
            else:
                labels[key] = np.array([])

            # Augment image and labels
            if self.augment:
                img, labels[key], M = random_affine(img, labels[key], degrees=(-5, 5), translate=(0.10, 0.10), scale=(0.50, 1.20))

            nL = len(labels[key])
            if nL > 0:
                # convert xyxy to xywh
                labels[key][:, 2:6] = xyxy2xywh(labels[key][:, 2:6].copy())  # / height
                labels[key][:, 2] /= width
                labels[key][:, 3] /= height
                labels[key][:, 4] /= width
                labels[key][:, 5] /= height

            if not unsup and self.augment:
                # random left-right flip during supervised learning
                if random.random() > 0.5:
                    img = np.fliplr(img)
                    if nL > 0:
                        labels[key][:, 2] = 1 - labels[key][:, 2]

            img = np.ascontiguousarray(img[:, :, ::-1])  # BGR to RGB

            if self.transforms is not None:
                img = self.transforms(img)

            images[key] = img

        if 'flipped' not in images:
            images['flipped'] = None
            labels['flipped'] = None

        return images, labels, img_path, (h, w)

    def format_gt_det(self, gt_det):
        if len(gt_det['scores']) == 0:
            gt_det = {'bboxes': np.array([[0, 0, 1, 1]], dtype=np.float32),
                      'scores': np.array([1], dtype=np.float32),
                      'clses': np.array([0], dtype=np.float32),
                      'cts': np.array([[0, 0]], dtype=np.float32),
                      'flipped_bboxes': np.array([[0, 0, 1, 1]], dtype=np.float32),
                      'flipped_cts': np.array([[0, 0]], dtype=np.float32)}

        gt_det = {k: np.array(gt_det[k], dtype=np.float32) for k in gt_det}
        return gt_det

    def __len__(self):
        return self.nF  # number of images


def letterbox(img, height=608, width=1088, color=(127.5, 127.5, 127.5)):
    # resize a rectangular image to a padded rectangular

    shape = img.shape[:2]  # shape = [height, width]
    ratio = min(float(height) / shape[0], float(width) / shape[1])
    new_shape = (round(shape[1] * ratio), round(shape[0] * ratio))  # new_shape = [width, height]
    dw = (width - new_shape[0]) / 2  # width padding
    dh = (height - new_shape[1]) / 2  # height padding
    top, bottom = round(dh - 0.1), round(dh + 0.1)
    left, right = round(dw - 0.1), round(dw + 0.1)
    img = cv2.resize(img, new_shape, interpolation=cv2.INTER_AREA)  # resized, no border
    img = cv2.copyMakeBorder(img, top, bottom, left, right, cv2.BORDER_CONSTANT, value=color)  # padded rectangular
    return img, ratio, dw, dh


def random_affine(img, targets=None, degrees=(-10, 10), translate=(.1, .1), scale=(.9, 1.1), shear=(-2, 2),
                  borderValue=(127.5, 127.5, 127.5)):
    # torchvision.transforms.RandomAffine(degrees=(-10, 10), translate=(.1, .1), scale=(.9, 1.1), shear=(-10, 10))
    # https://medium.com/uruvideo/dataset-augmentation-with-random-homographies-a8f4b44830d4

    border = 0  # width of added border (optional)
    height = img.shape[0]
    width = img.shape[1]

    # Rotation and Scale
    R = np.eye(3)
    a = random.random() * (degrees[1] - degrees[0]) + degrees[0]
    # a += random.choice([-180, -90, 0, 90])  # 90deg rotations added to small rotations
    s = random.random() * (scale[1] - scale[0]) + scale[0]
    R[:2] = cv2.getRotationMatrix2D(angle=a, center=(img.shape[1] / 2, img.shape[0] / 2), scale=s)

    # Translation
    T = np.eye(3)
    T[0, 2] = (random.random() * 2 - 1) * translate[0] * img.shape[0] + border  # x translation (pixels)
    T[1, 2] = (random.random() * 2 - 1) * translate[1] * img.shape[1] + border  # y translation (pixels)

    # Shear
    S = np.eye(3)
    S[0, 1] = math.tan((random.random() * (shear[1] - shear[0]) + shear[0]) * math.pi / 180)  # x shear (deg)
    S[1, 0] = math.tan((random.random() * (shear[1] - shear[0]) + shear[0]) * math.pi / 180)  # y shear (deg)

    M = S @ T @ R  # Combined rotation matrix. ORDER IS IMPORTANT HERE!!
    imw = cv2.warpPerspective(img, M, dsize=(width, height), flags=cv2.INTER_LINEAR,
                              borderValue=borderValue)  # BGR order borderValue

    # Return warped points also
    if targets is not None:
        if len(targets) > 0:
            n = targets.shape[0]
            points = targets[:, 2:6].copy()
            area0 = (points[:, 2] - points[:, 0]) * (points[:, 3] - points[:, 1])

            # warp points
            xy = np.ones((n * 4, 3))
            xy[:, :2] = points[:, [0, 1, 2, 3, 0, 3, 2, 1]].reshape(n * 4, 2)  # x1y1, x2y2, x1y2, x2y1
            xy = (xy @ M.T)[:, :2].reshape(n, 8)

            # create new boxes
            x = xy[:, [0, 2, 4, 6]]
            y = xy[:, [1, 3, 5, 7]]
            xy = np.concatenate((x.min(1), y.min(1), x.max(1), y.max(1))).reshape(4, n).T

            # apply angle-based reduction
            radians = a * math.pi / 180
            reduction = max(abs(math.sin(radians)), abs(math.cos(radians))) ** 0.5
            x = (xy[:, 2] + xy[:, 0]) / 2
            y = (xy[:, 3] + xy[:, 1]) / 2
            w = (xy[:, 2] - xy[:, 0]) * reduction
            h = (xy[:, 3] - xy[:, 1]) * reduction
            xy = np.concatenate((x - w / 2, y - h / 2, x + w / 2, y + h / 2)).reshape(4, n).T

            # reject warped points outside of image
            np.clip(xy[:, 0], 0, width, out=xy[:, 0])
            np.clip(xy[:, 2], 0, width, out=xy[:, 2])
            np.clip(xy[:, 1], 0, height, out=xy[:, 1])
            np.clip(xy[:, 3], 0, height, out=xy[:, 3])
            w = xy[:, 2] - xy[:, 0]
            h = xy[:, 3] - xy[:, 1]
            area = w * h
            ar = np.maximum(w / (h + 1e-16), h / (w + 1e-16))
            i = (w > 4) & (h > 4) & (area / (area0 + 1e-16) > 0.1) & (ar < 10)

            targets = targets[i]
            targets[:, 2:6] = xy[i]

        return imw, targets, M
    else:
        return imw


def collate_fn(batch):
    imgs, labels, paths, sizes = zip(*batch)
    batch_size = len(labels)
    imgs = torch.stack(imgs, 0)
    max_box_len = max([l.shape[0] for l in labels])
    labels = [torch.from_numpy(l) for l in labels]
    filled_labels = torch.zeros(batch_size, max_box_len, 6)
    labels_len = torch.zeros(batch_size)

    for i in range(batch_size):
        isize = labels[i].shape[0]
        if len(labels[i]) > 0:
            filled_labels[i, :isize, :] = labels[i]
        labels_len[i] = isize

    return imgs, filled_labels, paths, sizes, labels_len.unsqueeze(1)


class JointDataset(LoadImagesAndLabels):  # for training
    default_resolution = [1088, 608]
    mean = None
    std = None
    num_classes = 1

    def __init__(self, opt, root, paths, img_size=(1088, 608), augment=False, transforms=None):
        self.opt = opt
        dataset_names = paths.keys()
        self.img_files = OrderedDict()
        self.label_files = OrderedDict()
        self.img_num = OrderedDict()
        self.tid_num = OrderedDict()
        self.tid_start_index = OrderedDict()
        self.num_classes = 1

        # If doing self-supervised training we need to create positive and negative
        # examples of each image
        self.unsup = opt.unsup

        # Get image and annotation file names
        for ds, path in paths.items():
            with open(path, 'r') as file:
                self.img_files[ds] = file.readlines()
                self.img_files[ds] = [osp.join(root, x.strip()) for x in self.img_files[ds]]
                self.img_files[ds] = list(filter(lambda x: len(x) > 0, self.img_files[ds]))

            self.label_files[ds] = [
                x.replace('images', 'labels_with_ids').replace('.png', '.txt').replace('.jpg', '.txt')
                for x in self.img_files[ds]]

        # Counting unique identities in each dataset
        for ds, label_paths in self.label_files.items():
            max_index = -1
            for lp in label_paths:
                lb = np.loadtxt(lp)
                if len(lb) < 1:
                    continue
                if len(lb.shape) < 2:
                    img_max = lb[1]
                else:
                    img_max = np.max(lb[:, 1])
                if img_max > max_index:
                    max_index = img_max
            self.tid_num[ds] = max_index + 1

        # Finding the first identity (unique object) in each dataset
        last_index = 0
        for i, (k, v) in enumerate(self.tid_num.items()):
            self.tid_start_index[k] = last_index
            last_index += v

        # Total identities in all datasets
        self.nID = int(last_index + 1)

        # Count images and find starting file index for each dataset
        self.cds = []
        self.nF = 0
        for i, (ds, img_files) in enumerate(self.img_files.items()):
            img_cnt = len(img_files)
            self.img_num[ds] = img_cnt
            self.nF += img_cnt
            self.cds.append(self.nF - img_cnt)

        self.width = img_size[0]
        self.height = img_size[1]
        self.max_objs = opt.K
        self.augment = augment
        self.transforms = transforms

        print('Dataset Summary')
        print('=' * 100)
        print('Images per dataset: {}'.format(self.img_num))
        print('Identities per dataset: {}'.format(self.tid_num))
        print('Total images: {}'.format(self.nF))
        print('Total identities: {}'.format(self.nID))
        print('=' * 100)

    def __getitem__(self, files_index):
        # Find which dataset this index falls in
        ds = None
        start_index = 0
        for i, c in enumerate(self.cds):
            if files_index >= c:
                ds = list(self.label_files.keys())[i]
                start_index = c

        # Get image and annotation file names
        img_path = self.img_files[ds][files_index - start_index]
        label_path = self.label_files[ds][files_index - start_index]

        img_dict, lbl_dict, img_path, (input_h, input_w) = self.get_data(img_path, label_path, self.unsup)

        img, labels = img_dict['orig'], lbl_dict['orig']
        flipped_img, flipped_labels = img_dict['flipped'], lbl_dict['flipped']

        # Offset object IDs with starting ID index for this dataset
        for i, _ in enumerate(labels):
            if labels[i, 1] > -1:
                labels[i, 1] += self.tid_start_index[ds]

        output_h = img.shape[1] // self.opt.down_ratio
        output_w = img.shape[2] // self.opt.down_ratio
        num_classes = self.num_classes

        if labels.shape[0] != flipped_labels.shape[0]:
            print(labels.shape[0], flipped_labels.shape[0])
        num_objs = labels.shape[0]

        # heat map representing object detections
        hm = np.zeros((num_classes, output_h, output_w), dtype=np.float32)

        # width and height of each object
        wh = np.zeros((self.max_objs, 2), dtype=np.float32)

        # object center offset due to resizing and decimal error
        reg = np.zeros((self.max_objs, 2), dtype=np.float32)

        # contains object centers
        ind = np.zeros((self.max_objs,), dtype=np.int64)

        # mask representing the gt number of objects in this frame
        reg_mask = np.zeros((self.max_objs,), dtype=np.uint8)

        # object IDs
        ids = np.zeros((self.max_objs,), dtype=np.int64)

        gt_det = {'bboxes': [], 'scores': [], 'clses': [], 'cts': []}

        draw_gaussian = draw_msra_gaussian if self.opt.mse_loss else draw_umich_gaussian

        # Build ground truth labels
        for k in range(num_objs):
            label = labels[k]  # [class, identity, x_center, y_center, width, height]
            bbox = label[2:]
            cls_id = int(label[0])
            bbox[[0, 2]] = bbox[[0, 2]] * output_w
            bbox[[1, 3]] = bbox[[1, 3]] * output_h
            bbox[0] = np.clip(bbox[0], 0, output_w - 1)
            bbox[1] = np.clip(bbox[1], 0, output_h - 1)
            h = bbox[3]
            w = bbox[2]

            if h > 0 and w > 0:
                radius = gaussian_radius((math.ceil(h), math.ceil(w)))
                radius = max(0, int(radius))
                radius = self.opt.hm_gauss if self.opt.mse_loss else radius
                ct = np.array([bbox[0], bbox[1]], dtype=np.float32)
                ct_int = ct.astype(np.int32)
                draw_gaussian(hm[cls_id], ct_int, radius)
                wh[k] = 1. * w, 1. * h
                ind[k] = ct_int[1] * output_w + ct_int[0]
                reg[k] = ct - ct_int
                reg_mask[k] = 1
                ids[k] = label[1]

                gt_det['bboxes'].append(
                    np.array([ct[0] - w / 2, ct[1] - h / 2,
                              ct[0] + w / 2, ct[1] + h / 2], dtype=np.float32))
                gt_det['scores'].append(1)
                gt_det['clses'].append(cls_id)
                gt_det['cts'].append(ct)

        ret = {'img': img, 'hm': hm, 'reg_mask': reg_mask, 'ind': ind, 'wh': wh, 'reg': reg, 'ids': ids}

        if flipped_img is not None and flipped_labels is not None:
            flipped_ind = np.zeros((self.max_objs,), dtype=np.int64)
            gt_det['flipped_bboxes'] = []
            gt_det['flipped_cts'] = []

            for k in range(num_objs):
                flipped_label = flipped_labels[k]
                bbox = flipped_label[2:]
                bbox[[0, 2]] = bbox[[0, 2]] * output_w
                bbox[[1, 3]] = bbox[[1, 3]] * output_h
                bbox[0] = np.clip(bbox[0], 0, output_w - 1)
                bbox[1] = np.clip(bbox[1], 0, output_h - 1)
                h = bbox[3]
                w = bbox[2]

                if h > 0 and w > 0:
                    ct = np.array([bbox[0], bbox[1]], dtype=np.float32)
                    ct_int = ct.astype(np.int32)
                    flipped_ind[k] = ct_int[1] * output_w + ct_int[0]

                    gt_det['flipped_bboxes'].append(
                        np.array([ct[0] - w / 2, ct[1] - h / 2,
                                  ct[0] + w / 2, ct[1] + h / 2], dtype=np.float32))
                    gt_det['flipped_cts'].append(ct)

            ret['flipped_img'] = flipped_img
            ret['flipped_ind'] = flipped_ind

        ret['num_objs'] = torch.tensor([num_objs])

        if self.opt.debug > 0:
            gt_det = self.format_gt_det(gt_det)
            meta = {'gt_det': gt_det, 'img_path': img_path}
            ret['meta'] = meta

        return ret


class DetDataset(LoadImagesAndLabels):  # for training
    def __init__(self, root, paths, img_size=(1088, 608), augment=False, transforms=None):

        dataset_names = paths.keys()
        self.img_files = OrderedDict()
        self.label_files = OrderedDict()
        self.tid_num = OrderedDict()
        self.tid_start_index = OrderedDict()
        for ds, path in paths.items():
            with open(path, 'r') as file:
                self.img_files[ds] = file.readlines()
                self.img_files[ds] = [osp.join(root, x.strip()) for x in self.img_files[ds]]
                self.img_files[ds] = list(filter(lambda x: len(x) > 0, self.img_files[ds]))

            self.label_files[ds] = [
                x.replace('images', 'labels_with_ids').replace('.png', '.txt').replace('.jpg', '.txt')
                for x in self.img_files[ds]]

        for ds, label_paths in self.label_files.items():
            max_index = -1
            for lp in label_paths:
                lb = np.loadtxt(lp)
                if len(lb) < 1:
                    continue
                if len(lb.shape) < 2:
                    img_max = lb[1]
                else:
                    img_max = np.max(lb[:, 1])
                if img_max > max_index:
                    max_index = img_max
            self.tid_num[ds] = max_index + 1

        last_index = 0
        for i, (k, v) in enumerate(self.tid_num.items()):
            self.tid_start_index[k] = last_index
            last_index += v

        self.nID = int(last_index + 1)
        self.nds = [len(x) for x in self.img_files.values()]
        self.cds = [sum(self.nds[:i]) for i in range(len(self.nds))]
        self.nF = sum(self.nds)
        self.width = img_size[0]
        self.height = img_size[1]
        self.augment = augment
        self.transforms = transforms

        print('=' * 80)
        print('dataset summary')
        print(self.tid_num)
        print('total # identities:', self.nID)
        print('start index')
        print(self.tid_start_index)
        print('=' * 80)

    def __getitem__(self, files_index):

        for i, c in enumerate(self.cds):
            if files_index >= c:
                ds = list(self.label_files.keys())[i]
                start_index = c

        img_path = self.img_files[ds][files_index - start_index]
        label_path = self.label_files[ds][files_index - start_index]
        if os.path.isfile(label_path):
            labels0 = np.loadtxt(label_path, dtype=np.float32).reshape(-1, 6)

        imgs, labels, img_path, (h, w) = self.get_data(img_path, label_path)
        for i, _ in enumerate(labels):
            if labels[i, 1] > -1:
                labels[i, 1] += self.tid_start_index[ds]

        return imgs['orig'], labels0, img_path, (h, w)
