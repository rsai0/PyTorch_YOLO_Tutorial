#!/usr/bin/env python3
# -*- coding:utf-8 -*-

import torch
import torch.nn as nn

from .loss import build_criterion
from .yolox import YOLOX


# build object detector
def build_yolox(args, cfg, device, num_classes=80, trainable=False, deploy=False):
    print('==============================')
    print('Build {} ...'.format(args.model.upper()))
    
    print('==============================')
    print('Model Configuration: \n', cfg)
    
    # -------------- Build YOLO --------------
    model = YOLOX(
        cfg=cfg,
        device=device, 
        num_classes=num_classes,
        trainable=trainable,
        conf_thresh=args.conf_thresh,
        nms_thresh=args.nms_thresh,
        topk=args.topk,
        deploy=deploy
        )

    # -------------- Initialize YOLO --------------
    for m in model.modules():
        if isinstance(m, nn.BatchNorm2d):
            m.eps = 1e-3
            m.momentum = 0.03    

    # -------------- Build criterion --------------
    criterion = None
    if trainable:
        # build criterion for training
        criterion = build_criterion(cfg, device, num_classes)
    return model, criterion
