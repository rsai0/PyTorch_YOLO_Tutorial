# --------------- Torch components ---------------
import torch
import torch.nn as nn
import torch.nn.functional as F

# --------------- Model components ---------------
from .yolovx_backbone import build_backbone
from .yolovx_neck import build_neck
from .yolovx_pafpn import build_fpn
from .yolovx_head import build_det_head
from .yolovx_pred import build_pred_layer

# --------------- External components ---------------
from utils.misc import multiclass_nms


# YOLOvx
class YOLOvx(nn.Module):
    def __init__(self, 
                 cfg,
                 device, 
                 num_classes = 20, 
                 conf_thresh = 0.05,
                 nms_thresh = 0.6,
                 trainable = False, 
                 topk = 1000,
                 deploy = False):
        super(YOLOvx, self).__init__()
        # ---------------------- Basic Parameters ----------------------
        self.cfg = cfg
        self.device = device
        self.stride = cfg['stride']
        self.num_classes = num_classes
        self.trainable = trainable
        self.conf_thresh = conf_thresh
        self.nms_thresh = nms_thresh
        self.topk = topk
        self.deploy = deploy
        self.head_dim = round(256*cfg['width'])
        
        # ---------------------- Network Parameters ----------------------
        ## ----------- Backbone -----------
        self.backbone, feats_dim = build_backbone(cfg, trainable&cfg['pretrained'])

        ## ----------- Neck: SPP -----------
        self.neck = build_neck(cfg=cfg, in_dim=feats_dim[-1], out_dim=feats_dim[-1])
        feats_dim[-1] = self.neck.out_dim
        
        ## ----------- Neck: FPN -----------
        self.fpn = build_fpn(cfg=cfg, in_dims=feats_dim, out_dim=round(256*cfg['width']))
        self.fpn_dims = self.fpn.out_dim

        ## ----------- Heads -----------
        self.det_heads = build_det_head(
            cfg, self.fpn_dims, self.head_dim, num_classes, num_levels=len(self.stride))

        ## ----------- Preds -----------
        self.pred_layers = build_pred_layer(
            self.det_heads.cls_head_dim, self.det_heads.reg_head_dim,
            self.stride, num_classes, num_coords=4, num_levels=len(self.stride))


    ## post-process
    def post_process(self, obj_preds, cls_preds, box_preds):
        """
        Input:
            obj_preds: List(Tensor) [[H x W, 1], ...]
            cls_preds: List(Tensor) [[H x W, C], ...]
            box_preds: List(Tensor) [[H x W, 4], ...]
            anchors:   List(Tensor) [[H x W, 2], ...]
        """
        all_scores = []
        all_labels = []
        all_bboxes = []
        
        for obj_pred_i, cls_pred_i, box_pred_i in zip(obj_preds, cls_preds, box_preds):
            obj_pred_i = obj_pred_i[0]
            cls_pred_i = cls_pred_i[0]
            box_pred_i = box_pred_i[0]
            
            # (H x W x KA x C,)
            scores_i = (torch.sqrt(obj_pred_i.sigmoid() * cls_pred_i.sigmoid())).flatten()

            # Keep top k top scoring indices only.
            num_topk = min(self.topk, box_pred_i.size(0))

            # torch.sort is actually faster than .topk (at least on GPUs)
            predicted_prob, topk_idxs = scores_i.sort(descending=True)
            topk_scores = predicted_prob[:num_topk]
            topk_idxs = topk_idxs[:num_topk]

            # filter out the proposals with low confidence score
            keep_idxs = topk_scores > self.conf_thresh
            scores = topk_scores[keep_idxs]
            topk_idxs = topk_idxs[keep_idxs]

            anchor_idxs = torch.div(topk_idxs, self.num_classes, rounding_mode='floor')
            labels = topk_idxs % self.num_classes

            bboxes = box_pred_i[anchor_idxs]

            all_scores.append(scores)
            all_labels.append(labels)
            all_bboxes.append(bboxes)

        scores = torch.cat(all_scores)
        labels = torch.cat(all_labels)
        bboxes = torch.cat(all_bboxes)

        # to cpu & numpy
        scores = scores.cpu().numpy()
        labels = labels.cpu().numpy()
        bboxes = bboxes.cpu().numpy()

        # nms
        scores, labels, bboxes = multiclass_nms(
            scores, labels, bboxes, self.nms_thresh, self.num_classes, False)

        return bboxes, scores, labels


    # ---------------------- Main Process for Inference ----------------------
    @torch.no_grad()
    def inference_single_image(self, x):
        # ---------------- Backbone ----------------
        pyramid_feats = self.backbone(x)

        # ---------------- Neck: SPP ----------------
        pyramid_feats[-1] = self.neck(pyramid_feats[-1])

        # ---------------- Neck: PaFPN ----------------
        pyramid_feats = self.fpn(pyramid_feats)

        # ---------------- Heads ----------------
        cls_feats, reg_feats = self.det_heads(pyramid_feats)

        # ---------------- Preds ----------------
        outputs = self.pred_layers(cls_feats, reg_feats)

        all_obj_preds = outputs['pred_obj']
        all_cls_preds = outputs['pred_cls']
        all_box_preds = outputs['pred_box']

        if self.deploy:
            obj_preds = torch.cat(all_obj_preds, dim=1)[0]
            cls_preds = torch.cat(all_cls_preds, dim=1)[0]
            box_preds = torch.cat(all_box_preds, dim=1)[0]
            scores = torch.sqrt(obj_preds.sigmoid() * cls_preds.sigmoid())
            bboxes = box_preds
            # [n_anchors_all, 4 + C]
            outputs = torch.cat([bboxes, scores], dim=-1)

            return outputs
        else:
            # post process
            bboxes, scores, labels = self.post_process(
                all_obj_preds, all_cls_preds, all_box_preds)
        
            return bboxes, scores, labels


    def forward(self, x):
        if not self.trainable:
            return self.inference_single_image(x)
        else:
            # ---------------- Backbone ----------------
            pyramid_feats = self.backbone(x)

            # ---------------- Neck: SPP ----------------
            pyramid_feats[-1] = self.neck(pyramid_feats[-1])

            # ---------------- Neck: PaFPN ----------------
            pyramid_feats = self.fpn(pyramid_feats)

            # ---------------- Heads ----------------
            cls_feats, reg_feats = self.det_heads(pyramid_feats)

            # ---------------- Preds ----------------
            outputs = self.pred_layers(cls_feats, reg_feats)
            
            return outputs 
