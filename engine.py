import torch
import torch.distributed as dist

import time
import os
import numpy as np
import random

# ----------------- Extra Components -----------------
from utils import distributed_utils
from utils.misc import ModelEMA, CollateFunc, build_dataloader
from utils.vis_tools import vis_data

# ----------------- Evaluator Components -----------------
from evaluator.build import build_evluator

# ----------------- Optimizer & LrScheduler Components -----------------
from utils.solver.optimizer import build_yolo_optimizer, build_detr_optimizer
from utils.solver.lr_scheduler import build_lr_scheduler

# ----------------- Dataset Components -----------------
from dataset.build import build_dataset, build_transform


# Trainer refered to YOLOv8
class YoloTrainer(object):
    def __init__(self, args, data_cfg, model_cfg, trans_cfg, device, model, criterion):
        # ------------------- basic parameters -------------------
        self.args = args
        self.epoch = 0
        self.best_map = -1.
        self.last_opt_step = 0
        self.device = device
        self.criterion = criterion
        self.heavy_eval = False

        # ---------------------------- Build Dataset & Model & Trans. Config ----------------------------
        self.data_cfg = data_cfg
        self.model_cfg = model_cfg
        self.trans_cfg = trans_cfg

        # ---------------------------- Build Transform ----------------------------
        self.train_transform, self.trans_cfg = build_transform(
            args=self.args, trans_config=self.trans_cfg, max_stride=self.model_cfg['max_stride'], is_train=True)
        self.val_transform, _ = build_transform(
            args=self.args, trans_config=self.trans_cfg, max_stride=self.model_cfg['max_stride'], is_train=False)

        # ---------------------------- Build Dataset & Dataloader ----------------------------
        self.dataset, self.dataset_info = build_dataset(self.args, self.data_cfg, self.trans_cfg, self.train_transform, is_train=True)
        world_size = distributed_utils.get_world_size()
        self.train_loader = build_dataloader(self.args, self.dataset, self.args.batch_size // world_size, CollateFunc())

        # ---------------------------- Build Evaluator ----------------------------
        self.evaluator = build_evluator(self.args, self.data_cfg, self.val_transform, self.device)

        # ---------------------------- Build Grad. Scaler ----------------------------
        self.scaler = torch.cuda.amp.GradScaler(enabled=self.args.fp16)

        # ---------------------------- Build Optimizer ----------------------------
        accumulate = max(1, round(64 / self.args.batch_size))
        self.model_cfg['weight_decay'] *= self.args.batch_size * accumulate / 64
        self.optimizer, self.start_epoch = build_yolo_optimizer(self.model_cfg, model, self.args.resume)

        # ---------------------------- Build LR Scheduler ----------------------------
        self.args.max_epoch += self.args.wp_epoch
        self.lr_scheduler, self.lf = build_lr_scheduler(self.model_cfg, self.optimizer, self.args.max_epoch)
        self.lr_scheduler.last_epoch = self.start_epoch - 1  # do not move
        if self.args.resume:
            self.lr_scheduler.step()

        # ---------------------------- Build Model-EMA ----------------------------
        if self.args.ema and distributed_utils.get_rank() in [-1, 0]:
            print('Build ModelEMA ...')
            self.model_ema = ModelEMA(
                model,
                self.model_cfg['ema_decay'],
                self.model_cfg['ema_tau'],
                self.start_epoch * len(self.train_loader))
        else:
            self.model_ema = None


    def train(self, model):
        for epoch in range(self.start_epoch, self.args.max_epoch):
            if self.args.distributed:
                self.train_loader.batch_sampler.sampler.set_epoch(epoch)

            # check second stage
            if epoch >= (self.args.max_epoch - self.model_cfg['no_aug_epoch'] - 1):
                # close mosaic augmentation
                if self.train_loader.dataset.mosaic_prob > 0.:
                    print('close Mosaic Augmentation ...')
                    self.train_loader.dataset.mosaic_prob = 0.
                    self.heavy_eval = True
                # close mixup augmentation
                if self.train_loader.dataset.mixup_prob > 0.:
                    print('close Mixup Augmentation ...')
                    self.train_loader.dataset.mixup_prob = 0.
                    self.heavy_eval = True

            # train one epoch
            self.train_one_epoch(model)

            # eval one epoch
            if self.heavy_eval:
                model_eval = model.module if self.args.distributed else model
                self.eval(model_eval)
            else:
                model_eval = model.module if self.args.distributed else model
                if (epoch % self.args.eval_epoch) == 0 or (epoch == self.args.max_epoch - 1):
                    self.eval(model_eval)


    def eval(self, model):
        # chech model
        model_eval = model if self.model_ema is None else self.model_ema.ema

        # path to save model
        path_to_save = os.path.join(self.args.save_folder, self.args.dataset, self.args.model)
        os.makedirs(path_to_save, exist_ok=True)

        if distributed_utils.is_main_process():
            # check evaluator
            if self.evaluator is None:
                print('No evaluator ... save model and go on training.')
                print('Saving state, epoch: {}'.format(self.epoch + 1))
                weight_name = '{}_no_eval.pth'.format(self.args.model)
                checkpoint_path = os.path.join(path_to_save, weight_name)
                torch.save({'model': model_eval.state_dict(),
                            'mAP': -1.,
                            'optimizer': self.optimizer.state_dict(),
                            'epoch': self.epoch,
                            'args': self.args}, 
                            checkpoint_path)               
            else:
                print('eval ...')
                # set eval mode
                model_eval.trainable = False
                model_eval.eval()

                # evaluate
                with torch.no_grad():
                    self.evaluator.evaluate(model_eval)

                # save model
                cur_map = self.evaluator.map
                if cur_map > self.best_map:
                    # update best-map
                    self.best_map = cur_map
                    # save model
                    print('Saving state, epoch:', self.epoch + 1)
                    weight_name = '{}_best.pth'.format(self.args.model)
                    checkpoint_path = os.path.join(path_to_save, weight_name)
                    torch.save({'model': model_eval.state_dict(),
                                'mAP': round(self.best_map*100, 1),
                                'optimizer': self.optimizer.state_dict(),
                                'epoch': self.epoch,
                                'args': self.args}, 
                                checkpoint_path)                      

                # set train mode.
                model_eval.trainable = True
                model_eval.train()

        if self.args.distributed:
            # wait for all processes to synchronize
            dist.barrier()


    def train_one_epoch(self, model):
        # basic parameters
        epoch_size = len(self.train_loader)
        img_size = self.args.img_size
        t0 = time.time()
        nw = epoch_size * self.args.wp_epoch
        accumulate = accumulate = max(1, round(64 / self.args.batch_size))

        # Train one epoch
        for iter_i, (images, targets) in enumerate(self.train_loader):
            ni = iter_i + self.epoch * epoch_size
            # Warmup
            if ni <= nw:
                xi = [0, nw]  # x interp
                accumulate = max(1, np.interp(ni, xi, [1, 64 / self.args.batch_size]).round())
                for j, x in enumerate(self.optimizer.param_groups):
                    # bias lr falls from 0.1 to lr0, all other lrs rise from 0.0 to lr0
                    x['lr'] = np.interp(
                        ni, xi, [self.model_cfg['warmup_bias_lr'] if j == 0 else 0.0, x['initial_lr'] * self.lf(self.epoch)])
                    if 'momentum' in x:
                        x['momentum'] = np.interp(ni, xi, [self.model_cfg['warmup_momentum'], self.model_cfg['momentum']])
                                
            # To device
            images = images.to(self.device, non_blocking=True).float() / 255.

            # Multi scale
            if self.args.multi_scale:
                images, targets, img_size = self.rescale_image_targets(
                    images, targets, self.model_cfg['stride'], self.args.min_box_size, self.model_cfg['multi_scale'])
            else:
                targets = self.refine_targets(targets, self.args.min_box_size)
                
            # Visualize train targets
            if self.args.vis_tgt:
                vis_data(images*255, targets)

            # Inference
            with torch.cuda.amp.autocast(enabled=self.args.fp16):
                outputs = model(images)
                # Compute loss
                loss_dict = self.criterion(outputs=outputs, targets=targets)
                losses = loss_dict['losses']
                losses *= images.shape[0]  # loss * bs

                loss_dict_reduced = distributed_utils.reduce_dict(loss_dict)

                if self.args.distributed:
                    # gradient averaged between devices in DDP mode
                    losses *= distributed_utils.get_world_size()

            # Backward
            self.scaler.scale(losses).backward()

            # Optimize
            if ni - self.last_opt_step >= accumulate:
                if self.model_cfg['clip_grad'] > 0:
                    # unscale gradients
                    self.scaler.unscale_(self.optimizer)
                    # clip gradients
                    torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=self.model_cfg['clip_grad'])
                # optimizer.step
                self.scaler.step(self.optimizer)
                self.scaler.update()
                self.optimizer.zero_grad()
                # ema
                if self.model_ema is not None:
                    self.model_ema.update(model)
                self.last_opt_step = ni

            # Logs
            if distributed_utils.is_main_process() and iter_i % 10 == 0:
                t1 = time.time()
                cur_lr = [param_group['lr']  for param_group in self.optimizer.param_groups]
                # basic infor
                log =  '[Epoch: {}/{}]'.format(self.epoch+1, self.args.max_epoch)
                log += '[Iter: {}/{}]'.format(iter_i, epoch_size)
                log += '[lr: {:.6f}]'.format(cur_lr[2])
                # loss infor
                for k in loss_dict_reduced.keys():
                    if k == 'losses' and self.args.distributed:
                        world_size = distributed_utils.get_world_size()
                        log += '[{}: {:.2f}]'.format(k, loss_dict[k] / world_size)
                    else:
                        log += '[{}: {:.2f}]'.format(k, loss_dict[k])

                # other infor
                log += '[time: {:.2f}]'.format(t1 - t0)
                log += '[size: {}]'.format(img_size)

                # print log infor
                print(log, flush=True)
                
                t0 = time.time()
        
        # LR Schedule
        self.lr_scheduler.step()
        self.epoch += 1
        

    def refine_targets(self, targets, min_box_size):
        # rescale targets
        for tgt in targets:
            boxes = tgt["boxes"].clone()
            labels = tgt["labels"].clone()
            # refine tgt
            tgt_boxes_wh = boxes[..., 2:] - boxes[..., :2]
            min_tgt_size = torch.min(tgt_boxes_wh, dim=-1)[0]
            keep = (min_tgt_size >= min_box_size)

            tgt["boxes"] = boxes[keep]
            tgt["labels"] = labels[keep]
        
        return targets


    def rescale_image_targets(self, images, targets, stride, min_box_size, multi_scale_range=[0.5, 1.5]):
        """
            Deployed for Multi scale trick.
        """
        if isinstance(stride, int):
            max_stride = stride
        elif isinstance(stride, list):
            max_stride = max(stride)

        # During training phase, the shape of input image is square.
        old_img_size = images.shape[-1]
        new_img_size = random.randrange(old_img_size * multi_scale_range[0], old_img_size * multi_scale_range[1] + max_stride)
        new_img_size = new_img_size // max_stride * max_stride  # size
        if new_img_size / old_img_size != 1:
            # interpolate
            images = torch.nn.functional.interpolate(
                                input=images, 
                                size=new_img_size, 
                                mode='bilinear', 
                                align_corners=False)
        # rescale targets
        for tgt in targets:
            boxes = tgt["boxes"].clone()
            labels = tgt["labels"].clone()
            boxes = torch.clamp(boxes, 0, old_img_size)
            # rescale box
            boxes[:, [0, 2]] = boxes[:, [0, 2]] / old_img_size * new_img_size
            boxes[:, [1, 3]] = boxes[:, [1, 3]] / old_img_size * new_img_size
            # refine tgt
            tgt_boxes_wh = boxes[..., 2:] - boxes[..., :2]
            min_tgt_size = torch.min(tgt_boxes_wh, dim=-1)[0]
            keep = (min_tgt_size >= min_box_size)

            tgt["boxes"] = boxes[keep]
            tgt["labels"] = labels[keep]

        return images, targets, new_img_size


# Trainer refered to RTMDet
class RTMTrainer(object):
    def __init__(self, args, data_cfg, model_cfg, trans_cfg, device, model, criterion):
        # ------------------- basic parameters -------------------
        self.args = args
        self.epoch = 0
        self.best_map = -1.
        self.device = device
        self.criterion = criterion
        self.heavy_eval = False

        # ---------------------------- Build Dataset & Model & Trans. Config ----------------------------
        self.data_cfg = data_cfg
        self.model_cfg = model_cfg
        self.trans_cfg = trans_cfg

        # ---------------------------- Build Transform ----------------------------
        self.train_transform, self.trans_cfg = build_transform(
            args=self.args, trans_config=self.trans_cfg, max_stride=self.model_cfg['max_stride'], is_train=True)
        self.val_transform, _ = build_transform(
            args=self.args, trans_config=self.trans_cfg, max_stride=self.model_cfg['max_stride'], is_train=False)

        # ---------------------------- Build Dataset & Dataloader ----------------------------
        self.dataset, self.dataset_info = build_dataset(self.args, self.data_cfg, self.trans_cfg, self.train_transform, is_train=True)
        world_size = distributed_utils.get_world_size()
        self.train_loader = build_dataloader(self.args, self.dataset, self.args.batch_size // world_size, CollateFunc())

        # ---------------------------- Build Evaluator ----------------------------
        self.evaluator = build_evluator(self.args, self.data_cfg, self.val_transform, self.device)

        # ---------------------------- Build Grad. Scaler ----------------------------
        self.scaler = torch.cuda.amp.GradScaler(enabled=self.args.fp16)

        # ---------------------------- Build Optimizer ----------------------------
        self.model_cfg['lr0'] *= self.args.batch_size / 64
        self.optimizer, self.start_epoch = build_yolo_optimizer(self.model_cfg, model, self.args.resume)

        # ---------------------------- Build LR Scheduler ----------------------------
        self.args.max_epoch += self.args.wp_epoch
        self.lr_scheduler, self.lf = build_lr_scheduler(self.model_cfg, self.optimizer, self.args.max_epoch)
        self.lr_scheduler.last_epoch = self.start_epoch - 1  # do not move
        if self.args.resume:
            self.lr_scheduler.step()

        # ---------------------------- Build Model-EMA ----------------------------
        if self.args.ema and distributed_utils.get_rank() in [-1, 0]:
            print('Build ModelEMA ...')
            self.model_ema = ModelEMA(
                model,
                self.model_cfg['ema_decay'],
                self.model_cfg['ema_tau'],
                self.start_epoch * len(self.train_loader))
        else:
            self.model_ema = None


    def train(self, model):
        for epoch in range(self.start_epoch, self.args.max_epoch):
            if self.args.distributed:
                self.train_loader.batch_sampler.sampler.set_epoch(epoch)

            # check second stage
            if epoch >= (self.args.max_epoch - self.model_cfg['no_aug_epoch'] - 1):
                # close mosaic augmentation
                if self.train_loader.dataset.mosaic_prob > 0.:
                    print('close Mosaic Augmentation ...')
                    self.train_loader.dataset.mosaic_prob = 0.
                    self.heavy_eval = True
                # close mixup augmentation
                if self.train_loader.dataset.mixup_prob > 0.:
                    print('close Mixup Augmentation ...')
                    self.train_loader.dataset.mixup_prob = 0.
                    self.heavy_eval = True

            # train one epoch
            self.train_one_epoch(model)

            # eval one epoch
            if self.heavy_eval:
                model_eval = model.module if self.args.distributed else model
                self.eval(model_eval)
            else:
                model_eval = model.module if self.args.distributed else model
                if (epoch % self.args.eval_epoch) == 0 or (epoch == self.args.max_epoch - 1):
                    self.eval(model_eval)


    def eval(self, model):
        # chech model
        model_eval = model if self.model_ema is None else self.model_ema.ema

        # path to save model
        path_to_save = os.path.join(self.args.save_folder, self.args.dataset, self.args.model)
        os.makedirs(path_to_save, exist_ok=True)

        if distributed_utils.is_main_process():
            # check evaluator
            if self.evaluator is None:
                print('No evaluator ... save model and go on training.')
                print('Saving state, epoch: {}'.format(self.epoch + 1))
                weight_name = '{}_no_eval.pth'.format(self.args.model)
                checkpoint_path = os.path.join(path_to_save, weight_name)
                torch.save({'model': model_eval.state_dict(),
                            'mAP': -1.,
                            'optimizer': self.optimizer.state_dict(),
                            'epoch': self.epoch,
                            'args': self.args}, 
                            checkpoint_path)               
            else:
                print('eval ...')
                # set eval mode
                model_eval.trainable = False
                model_eval.eval()

                # evaluate
                with torch.no_grad():
                    self.evaluator.evaluate(model_eval)

                # save model
                cur_map = self.evaluator.map
                if cur_map > self.best_map:
                    # update best-map
                    self.best_map = cur_map
                    # save model
                    print('Saving state, epoch:', self.epoch + 1)
                    weight_name = '{}_best.pth'.format(self.args.model)
                    checkpoint_path = os.path.join(path_to_save, weight_name)
                    torch.save({'model': model_eval.state_dict(),
                                'mAP': round(self.best_map*100, 1),
                                'optimizer': self.optimizer.state_dict(),
                                'epoch': self.epoch,
                                'args': self.args}, 
                                checkpoint_path)                      

                # set train mode.
                model_eval.trainable = True
                model_eval.train()

        if self.args.distributed:
            # wait for all processes to synchronize
            dist.barrier()


    def train_one_epoch(self, model):
        # basic parameters
        epoch_size = len(self.train_loader)
        img_size = self.args.img_size
        t0 = time.time()
        nw = epoch_size * self.args.wp_epoch

        # Train one epoch
        for iter_i, (images, targets) in enumerate(self.train_loader):
            ni = iter_i + self.epoch * epoch_size
            # Warmup
            if ni <= nw:
                xi = [0, nw]  # x interp
                for j, x in enumerate(self.optimizer.param_groups):
                    # bias lr falls from 0.1 to lr0, all other lrs rise from 0.0 to lr0
                    x['lr'] = np.interp(
                        ni, xi, [self.model_cfg['warmup_bias_lr'] if j == 0 else 0.0, x['initial_lr'] * self.lf(self.epoch)])
                    if 'momentum' in x:
                        x['momentum'] = np.interp(ni, xi, [self.model_cfg['warmup_momentum'], self.model_cfg['momentum']])
                                
            # To device
            images = images.to(self.device, non_blocking=True).float() / 255.

            # Multi scale
            if self.args.multi_scale:
                images, targets, img_size = self.rescale_image_targets(
                    images, targets, self.model_cfg['stride'], self.args.min_box_size, self.model_cfg['multi_scale'])
            else:
                targets = self.refine_targets(targets, self.args.min_box_size)
                
            # Visualize train targets
            if self.args.vis_tgt:
                vis_data(images*255, targets)

            # Inference
            with torch.cuda.amp.autocast(enabled=self.args.fp16):
                outputs = model(images)
                # Compute loss
                loss_dict = self.criterion(outputs=outputs, targets=targets)
                losses = loss_dict['losses']

                loss_dict_reduced = distributed_utils.reduce_dict(loss_dict)

            # Backward
            self.scaler.scale(losses).backward()

            # Optimize
            if self.model_cfg['clip_grad'] > 0:
                # unscale gradients
                self.scaler.unscale_(self.optimizer)
                # clip gradients
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=self.model_cfg['clip_grad'])
            # optimizer.step
            self.scaler.step(self.optimizer)
            self.scaler.update()
            self.optimizer.zero_grad()
            # ema
            if self.model_ema is not None:
                self.model_ema.update(model)

            # Logs
            if distributed_utils.is_main_process() and iter_i % 10 == 0:
                t1 = time.time()
                cur_lr = [param_group['lr']  for param_group in self.optimizer.param_groups]
                # basic infor
                log =  '[Epoch: {}/{}]'.format(self.epoch+1, self.args.max_epoch)
                log += '[Iter: {}/{}]'.format(iter_i, epoch_size)
                log += '[lr: {:.6f}]'.format(cur_lr[2])
                # loss infor
                for k in loss_dict_reduced.keys():
                    log += '[{}: {:.2f}]'.format(k, loss_dict[k])

                # other infor
                log += '[time: {:.2f}]'.format(t1 - t0)
                log += '[size: {}]'.format(img_size)

                # print log infor
                print(log, flush=True)
                
                t0 = time.time()
        
        # LR Schedule
        self.lr_scheduler.step()
        self.epoch += 1
        

    def refine_targets(self, targets, min_box_size):
        # rescale targets
        for tgt in targets:
            boxes = tgt["boxes"].clone()
            labels = tgt["labels"].clone()
            # refine tgt
            tgt_boxes_wh = boxes[..., 2:] - boxes[..., :2]
            min_tgt_size = torch.min(tgt_boxes_wh, dim=-1)[0]
            keep = (min_tgt_size >= min_box_size)

            tgt["boxes"] = boxes[keep]
            tgt["labels"] = labels[keep]
        
        return targets


    def rescale_image_targets(self, images, targets, stride, min_box_size, multi_scale_range=[0.5, 1.5]):
        """
            Deployed for Multi scale trick.
        """
        if isinstance(stride, int):
            max_stride = stride
        elif isinstance(stride, list):
            max_stride = max(stride)

        # During training phase, the shape of input image is square.
        old_img_size = images.shape[-1]
        new_img_size = random.randrange(old_img_size * multi_scale_range[0], old_img_size * multi_scale_range[1] + max_stride)
        new_img_size = new_img_size // max_stride * max_stride  # size
        if new_img_size / old_img_size != 1:
            # interpolate
            images = torch.nn.functional.interpolate(
                                input=images, 
                                size=new_img_size, 
                                mode='bilinear', 
                                align_corners=False)
        # rescale targets
        for tgt in targets:
            boxes = tgt["boxes"].clone()
            labels = tgt["labels"].clone()
            boxes = torch.clamp(boxes, 0, old_img_size)
            # rescale box
            boxes[:, [0, 2]] = boxes[:, [0, 2]] / old_img_size * new_img_size
            boxes[:, [1, 3]] = boxes[:, [1, 3]] / old_img_size * new_img_size
            # refine tgt
            tgt_boxes_wh = boxes[..., 2:] - boxes[..., :2]
            min_tgt_size = torch.min(tgt_boxes_wh, dim=-1)[0]
            keep = (min_tgt_size >= min_box_size)

            tgt["boxes"] = boxes[keep]
            tgt["labels"] = labels[keep]

        return images, targets, new_img_size


# Trainer for DETR
class DetrTrainer(object):
    def __init__(self, args, data_cfg, model_cfg, trans_cfg, device, model, criterion):
        # ------------------- basic parameters -------------------
        self.args = args
        self.epoch = 0
        self.best_map = -1.
        self.last_opt_step = 0
        self.device = device
        self.criterion = criterion
        self.heavy_eval = False

        # ---------------------------- Build Dataset & Model & Trans. Config ----------------------------
        self.data_cfg = data_cfg
        self.model_cfg = model_cfg
        self.trans_cfg = trans_cfg

        # ---------------------------- Build Transform ----------------------------
        self.train_transform, self.trans_cfg = build_transform(
            args=self.args, trans_config=self.trans_cfg, max_stride=self.model_cfg['max_stride'], is_train=True)
        self.val_transform, _ = build_transform(
            args=self.args, trans_config=self.trans_cfg, max_stride=self.model_cfg['max_stride'], is_train=False)

        # ---------------------------- Build Dataset & Dataloader ----------------------------
        self.dataset, self.dataset_info = build_dataset(self.args, self.data_cfg, self.trans_cfg, self.train_transform, is_train=True)
        world_size = distributed_utils.get_world_size()
        self.train_loader = build_dataloader(self.args, self.dataset, self.args.batch_size // world_size, CollateFunc())

        # ---------------------------- Build Evaluator ----------------------------
        self.evaluator = build_evluator(self.args, self.data_cfg, self.val_transform, self.device)

        # ---------------------------- Build Grad. Scaler ----------------------------
        self.scaler = torch.cuda.amp.GradScaler(enabled=self.args.fp16)

        # ---------------------------- Build Optimizer ----------------------------
        self.model_cfg['lr0'] *= self.args.batch_size / 16.
        self.optimizer, self.start_epoch = build_detr_optimizer(model_cfg, model, self.args.resume)

        # ---------------------------- Build LR Scheduler ----------------------------
        self.args.max_epoch += self.args.wp_epoch
        self.lr_scheduler, self.lf = build_lr_scheduler(self.model_cfg, self.optimizer, self.args.max_epoch)
        self.lr_scheduler.last_epoch = self.start_epoch - 1  # do not move
        if self.args.resume:
            self.lr_scheduler.step()

        # ---------------------------- Build Model-EMA ----------------------------
        if self.args.ema and distributed_utils.get_rank() in [-1, 0]:
            print('Build ModelEMA ...')
            self.model_ema = ModelEMA(
                model,
                self.model_cfg['ema_decay'],
                self.model_cfg['ema_tau'],
                self.start_epoch * len(self.train_loader))
        else:
            self.model_ema = None


    def train(self, model):
        for epoch in range(self.start_epoch, self.args.max_epoch):
            if self.args.distributed:
                self.train_loader.batch_sampler.sampler.set_epoch(epoch)

            # check second stage
            if epoch >= (self.args.max_epoch - self.model_cfg['no_aug_epoch'] - 1):
                # close mosaic augmentation
                if self.train_loader.dataset.mosaic_prob > 0.:
                    print('close Mosaic Augmentation ...')
                    self.train_loader.dataset.mosaic_prob = 0.
                    self.heavy_eval = True
                # close mixup augmentation
                if self.train_loader.dataset.mixup_prob > 0.:
                    print('close Mixup Augmentation ...')
                    self.train_loader.dataset.mixup_prob = 0.
                    self.heavy_eval = True

            # train one epoch
            self.train_one_epoch(model)

            # eval one epoch
            if self.heavy_eval:
                model_eval = model.module if self.args.distributed else model
                self.eval(model_eval)
            else:
                model_eval = model.module if self.args.distributed else model
                if (epoch % self.args.eval_epoch) == 0 or (epoch == self.args.max_epoch - 1):
                    self.eval(model_eval)


    def eval(self, model):
        # chech model
        model_eval = model if self.model_ema is None else self.model_ema.ema

        # path to save model
        path_to_save = os.path.join(self.args.save_folder, self.args.dataset, self.args.model)
        os.makedirs(path_to_save, exist_ok=True)

        if distributed_utils.is_main_process():
            # check evaluator
            if self.evaluator is None:
                print('No evaluator ... save model and go on training.')
                print('Saving state, epoch: {}'.format(self.epoch + 1))
                weight_name = '{}_no_eval.pth'.format(self.args.model)
                checkpoint_path = os.path.join(path_to_save, weight_name)
                torch.save({'model': model_eval.state_dict(),
                            'mAP': -1.,
                            'optimizer': self.optimizer.state_dict(),
                            'epoch': self.epoch,
                            'args': self.args}, 
                            checkpoint_path)  
            else:
                print('eval ...')
                # set eval mode
                model_eval.trainable = False
                model_eval.eval()

                # evaluate
                with torch.no_grad():
                    self.evaluator.evaluate(model_eval)

                # save model
                cur_map = self.evaluator.map
                if cur_map > self.best_map:
                    # update best-map
                    self.best_map = cur_map
                    # save model
                    print('Saving state, epoch:', self.epoch + 1)
                    weight_name = '{}_best.pth'.format(self.args.model)
                    checkpoint_path = os.path.join(path_to_save, weight_name)
                    torch.save({'model': model_eval.state_dict(),
                                'mAP': round(self.best_map*100, 1),
                                'optimizer': self.optimizer.state_dict(),
                                'epoch': self.epoch,
                                'args': self.args}, 
                                checkpoint_path)                      

                # set train mode.
                model_eval.trainable = True
                model_eval.train()

        if self.args.distributed:
            # wait for all processes to synchronize
            dist.barrier()


    def train_one_epoch(self, model):
        # basic parameters
        epoch_size = len(self.train_loader)
        img_size = self.args.img_size
        t0 = time.time()
        nw = epoch_size * self.args.wp_epoch

        # train one epoch
        for iter_i, (images, targets) in enumerate(self.train_loader):
            ni = iter_i + self.epoch * epoch_size
            # Warmup
            if ni <= nw:
                xi = [0, nw]  # x interp
                for j, x in enumerate(self.optimizer.param_groups):
                    # bias lr falls from 0.1 to lr0, all other lrs rise from 0.0 to lr0
                    x['lr'] = np.interp(
                        ni, xi, [0.0, x['initial_lr'] * self.lf(self.epoch)])
                    if 'momentum' in x:
                        x['momentum'] = np.interp(ni, xi, [self.model_cfg['warmup_momentum'], self.model_cfg['momentum']])
                                
            # To device
            images = images.to(self.device, non_blocking=True).float() / 255.

            # Multi scale
            if self.args.multi_scale:
                images, targets, img_size = self.rescale_image_targets(
                    images, targets, model.max_stride, self.args.min_box_size, self.model_cfg['multi_scale'])
            else:
                targets = self.refine_targets(targets, self.args.min_box_size, img_size)
                
            # Visualize targets
            if self.args.vis_tgt:
                vis_data(images*255, targets)

            # Inference
            with torch.cuda.amp.autocast(enabled=self.args.fp16):
                outputs = model(images)
                # Compute loss
                loss_dict = self.criterion(outputs=outputs, targets=targets)
                losses = loss_dict['losses']

                loss_dict_reduced = distributed_utils.reduce_dict(loss_dict)

            # Backward
            self.scaler.scale(losses).backward()

            # Optimize
            if self.model_cfg['clip_grad'] > 0:
                # unscale gradients
                self.scaler.unscale_(self.optimizer)
                # clip gradients
                torch.nn.utils.clip_grad_norm_(model.parameters(), max_norm=self.model_cfg['clip_grad'])
            self.scaler.step(self.optimizer)
            self.scaler.update()
            self.optimizer.zero_grad()

            # Model EMA
            if self.model_ema is not None:
                self.model_ema.update(model)
            self.last_opt_step = ni

            # Log
            if distributed_utils.is_main_process() and iter_i % 10 == 0:
                t1 = time.time()
                cur_lr = [param_group['lr']  for param_group in self.optimizer.param_groups]
                # basic infor
                log =  '[Epoch: {}/{}]'.format(self.epoch+1, self.args.max_epoch)
                log += '[Iter: {}/{}]'.format(iter_i, epoch_size)
                log += '[lr: {:.6f}]'.format(cur_lr[0])
                # loss infor
                for k in loss_dict_reduced.keys():
                    if self.args.vis_aux_loss:
                        log += '[{}: {:.2f}]'.format(k, loss_dict[k])
                    else:
                        if k in ['loss_cls', 'loss_bbox', 'loss_giou', 'losses']:
                            log += '[{}: {:.2f}]'.format(k, loss_dict[k])

                # other infor
                log += '[time: {:.2f}]'.format(t1 - t0)
                log += '[size: {}]'.format(img_size)

                # print log infor
                print(log, flush=True)
                
                t0 = time.time()
        
        # LR Scheduler
        self.lr_scheduler.step()
        self.epoch += 1
        

    def refine_targets(self, targets, min_box_size, img_size):
        # rescale targets
        for tgt in targets:
            boxes = tgt["boxes"]
            labels = tgt["labels"]
            # refine tgt
            tgt_boxes_wh = boxes[..., 2:] - boxes[..., :2]
            min_tgt_size = torch.min(tgt_boxes_wh, dim=-1)[0]
            keep = (min_tgt_size >= min_box_size)
            # xyxy -> cxcywh
            new_boxes = torch.zeros_like(boxes)
            new_boxes[..., :2] = (boxes[..., 2:] + boxes[..., :2]) * 0.5
            new_boxes[..., 2:] = (boxes[..., 2:] - boxes[..., :2])
            # normalize
            new_boxes /= img_size
            del boxes

            tgt["boxes"] = new_boxes[keep]
            tgt["labels"] = labels[keep]
        
        return targets


    def rescale_image_targets(self, images, targets, stride, min_box_size, multi_scale_range=[0.5, 1.5]):
        """
            Deployed for Multi scale trick.
        """
        if isinstance(stride, int):
            max_stride = stride
        elif isinstance(stride, list):
            max_stride = max(stride)

        # During training phase, the shape of input image is square.
        old_img_size = images.shape[-1]
        new_img_size = random.randrange(old_img_size * multi_scale_range[0], old_img_size * multi_scale_range[1] + max_stride)
        new_img_size = new_img_size // max_stride * max_stride  # size
        if new_img_size / old_img_size != 1:
            # interpolate
            images = torch.nn.functional.interpolate(
                                input=images, 
                                size=new_img_size, 
                                mode='bilinear', 
                                align_corners=False)
        # rescale targets
        for tgt in targets:
            boxes = tgt["boxes"].clone()
            labels = tgt["labels"].clone()
            boxes = torch.clamp(boxes, 0, old_img_size)
            # rescale box
            boxes[:, [0, 2]] = boxes[:, [0, 2]] / old_img_size * new_img_size
            boxes[:, [1, 3]] = boxes[:, [1, 3]] / old_img_size * new_img_size
            # refine tgt
            tgt_boxes_wh = boxes[..., 2:] - boxes[..., :2]
            min_tgt_size = torch.min(tgt_boxes_wh, dim=-1)[0]
            keep = (min_tgt_size >= min_box_size)
            # xyxy -> cxcywh
            new_boxes = torch.zeros_like(boxes)
            new_boxes[..., :2] = (boxes[..., 2:] + boxes[..., :2]) * 0.5
            new_boxes[..., 2:] = (boxes[..., 2:] - boxes[..., :2])
            # normalize
            new_boxes /= new_img_size
            del boxes

            tgt["boxes"] = new_boxes[keep]
            tgt["labels"] = labels[keep]

        return images, targets, new_img_size


# Build Trainer
def build_trainer(args, data_cfg, model_cfg, trans_cfg, device, model, criterion):
    if model_cfg['trainer_type'] == 'yolo':
        return YoloTrainer(args, data_cfg, model_cfg, trans_cfg, device, model, criterion)
    elif model_cfg['trainer_type'] == 'rtmdet':
        return RTMTrainer(args, data_cfg, model_cfg, trans_cfg, device, model, criterion)
    elif model_cfg['trainer_type'] == 'detr':
        return DetrTrainer(args, data_cfg, model_cfg, trans_cfg, device, model, criterion)
    else:
        raise NotImplementedError
    