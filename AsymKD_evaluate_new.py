from __future__ import print_function, division
import sys
sys.path.append('core')
import os
import cv2
 
import argparse
import time
import logging
import numpy as np
import torch
from tqdm import tqdm
#from raft_stereo import RAFTStereo, autocast
import dataset_raw_kitti
from torch.utils.data import DataLoader

import core.AsymKD_datasets as datasets
from core.utils import InputPadder
from segment_anything import  sam_model_registry, SamPredictor
from AsymKD.dpt import AsymKD_DepthAnything, AsymKD_DepthAnything_Infer
import torch.nn as nn
from depth_anything_for_evaluate.dpt import DepthAnything
import matplotlib.pyplot as plt
from AsymKD_student import AsymKD_Student_DPTHead, AsymKD_Student_Encoder, AsymKD_Student

 
 
def readlines(filename):
    """Read all the lines in a text file and return as a list
    """
    with open(filename, 'r') as f:
        lines = f.read().splitlines()
    return lines

class RunningAverage:
    def __init__(self):
        self.avg = 0
        self.count = 0
 
    def append(self, value):
        self.avg = (value + self.count * self.avg) / (self.count + 1)
        self.count += 1
 
    def get_value(self):
        return self.avg
 
 
class RunningAverageDict:
    """A dictionary of running averages."""
    def __init__(self):
        self._dict = None
 
    def update(self, new_dict):
        if new_dict is None:
            return
 
        if self._dict is None:
            self._dict = dict()
            for key, value in new_dict.items():
                self._dict[key] = RunningAverage()
 
        for key, value in new_dict.items():
            self._dict[key].append(value)
 
    def get_value(self):
        if self._dict is None:
            return None
        return {key: value.get_value() for key, value in self._dict.items()}
 
 
def compute_errors(gt, pred):
    """Compute metrics for 'pred' compared to 'gt'
 
    Args:
        gt (numpy.ndarray): Ground truth values
        pred (numpy.ndarray): Predicted values
 
        gt.shape should be equal to pred.shape
 
    Returns:
        dict: Dictionary containing the following metrics:
            'a1': Delta1 accuracy: Fraction of pixels that are within a scale factor of 1.25
            'a2': Delta2 accuracy: Fraction of pixels that are within a scale factor of 1.25^2
            'a3': Delta3 accuracy: Fraction of pixels that are within a scale factor of 1.25^3
            'abs_rel': Absolute relative error
            'rmse': Root mean squared error
            'log_10': Absolute log10 error
            'sq_rel': Squared relative error
            'rmse_log': Root mean squared error on the log scale
            'silog': Scale invariant log error
    """
    thresh = np.maximum((gt / pred), (pred / gt))
    a1 = (thresh < 1.25).mean()
    a2 = (thresh < 1.25 ** 2).mean()
    a3 = (thresh < 1.25 ** 3).mean()
 
    abs_rel = np.mean(np.abs(gt - pred) / gt)
    sq_rel = np.mean(((gt - pred) ** 2) / gt)
 
    rmse = (gt - pred) ** 2
    rmse = np.sqrt(rmse.mean())
 
    rmse_log = (np.log(gt) - np.log(pred)) ** 2
    rmse_log = np.sqrt(rmse_log.mean())
 
    err = np.log(pred) - np.log(gt)
    silog = np.sqrt(np.mean(err ** 2) - np.mean(err) ** 2) * 100
 
    log_10 = (np.abs(np.log10(gt) - np.log10(pred))).mean()
    # print(f'a1 : {a1}')
    return dict(a1=a1, a2=a2, a3=a3, abs_rel=abs_rel, rmse=rmse, log_10=log_10, rmse_log=rmse_log,
                silog=silog, sq_rel=sq_rel)
 
 
def compute_metrics(gt, pred, interpolate=True, garg_crop=True, eigen_crop=False, dataset='kitti', min_depth_eval=1e-3, max_depth_eval=80):
    """Compute metrics of predicted depth maps. Applies cropping and masking as necessary or specified via arguments. Refer to compute_errors for more details on metrics.
    """
    if gt.shape[-2:] != pred.shape[-2:] and interpolate:
        pred = nn.functional.interpolate(
            pred, gt.shape[-2:], mode='bilinear', align_corners=True)
    
    pred = pred.squeeze().cpu().numpy()
    # pred[pred < min_depth_eval] = min_depth_eval
    # pred[pred > max_depth_eval] = max_depth_eval
    # pred[np.isinf(pred)] = max_depth_eval
    # pred[np.isnan(pred)] = min_depth_eval
 
    gt_depth = gt.squeeze().cpu().numpy()

    pred = 1 / pred

    valid_mask = np.logical_and(
        gt_depth > min_depth_eval, gt_depth < max_depth_eval)
 
    if garg_crop or eigen_crop:
        gt_height, gt_width = gt_depth.shape
        eval_mask = np.zeros(valid_mask.shape)
 
        if garg_crop:
            eval_mask[int(0.40810811 * gt_height):int(0.99189189 * gt_height),
                      int(0.03594771 * gt_width):int(0.96405229 * gt_width)] = 1
 
        elif eigen_crop:
            # print("-"*10, " EIGEN CROP ", "-"*10)
            if dataset == 'kitti':
                eval_mask[int(0.3324324 * gt_height):int(0.91351351 * gt_height),
                          int(0.0359477 * gt_width):int(0.96405229 * gt_width)] = 1
            else:
                # assert gt_depth.shape == (480, 640), "Error: Eigen crop is currently only valid for (480, 640) images"
                eval_mask[45:471, 41:601] = 1
        else:
            eval_mask = np.ones(valid_mask.shape)
    valid_mask = np.logical_and(valid_mask, eval_mask)
 
    # median scaling
    pred = pred[valid_mask]
    gt_depth = gt_depth[valid_mask]
 
    ratio = np.median(gt_depth) / np.median(pred)
    pred *= ratio
 
    pred[pred < min_depth_eval] = min_depth_eval
    pred[pred > max_depth_eval] = max_depth_eval
    pred[np.isinf(pred)] = max_depth_eval
    pred[np.isnan(pred)] = min_depth_eval
 
    # return compute_errors(gt_depth[valid_mask], pred[valid_mask])
    return compute_errors(gt_depth, pred)
 
 
@torch.no_grad()
def infer(model, images, **kwargs):
    """Inference with flip augmentation"""
    # images.shape = N, C, H, W
    def get_depth_from_prediction(pred):
        if isinstance(pred, torch.Tensor):
            pred = pred  # pass
        elif isinstance(pred, (list, tuple)):
            pred = pred[-1]
        elif isinstance(pred, dict):
            pred = pred['metric_depth'] if 'metric_depth' in pred else pred['out']
        else:
            raise NotImplementedError(f"Unknown output type {type(pred)}")
        return pred
 
    pred1 = model(images, **kwargs)
    pred1 = get_depth_from_prediction(pred1)
 
    pred2 = model(torch.flip(images, [3]), **kwargs)
    pred2 = get_depth_from_prediction(pred2)
    pred2 = torch.flip(pred2, [3])
 
    mean_pred = 0.5 * (pred1 + pred2)
 
    return mean_pred
 
 
@torch.no_grad()
def validate_kitti(model, seg_any_predictor: SamPredictor, round_vals=True, round_precision=3):
    """ Peform validation using the KITTI-2015 (train) split """
    model.eval()
    val_dataset = datasets.KITTI(seg_any_predictor=seg_any_predictor, aug_params={}, image_set='training')
    torch.backends.cudnn.benchmark = True
    metrics = RunningAverageDict()
 
    for sample in tqdm(val_dataset, total=len(val_dataset)):
        img_depth, img_seg, gt, valid = sample
        img_depth = img_depth[None].cuda()
        img_seg = img_seg[None].cuda()
 
        # pred = infer()
 
        flow_pr = model(img_depth, img_seg) # infer?
 
        metrics.update(compute_metrics(gt, flow_pr))
 
    if round_vals:
        def r(m): return round(m, round_precision)
    else:
        def r(m): return m
    metrics = {k: r(v) for k, v in metrics.get_value().items()}
   
    return metrics

@torch.no_grad()
def validate_kitti_for_depth_anything(model, seg_any_predictor: SamPredictor, round_vals=True, round_precision=3):
    """ Peform validation using the KITTI-2015 (train) split """
    model.eval()
    val_dataset = datasets.KITTI(seg_any_predictor=seg_any_predictor, aug_params={}, image_set='training')
    torch.backends.cudnn.benchmark = True
    metrics = RunningAverageDict()
 
    for val_id in tqdm(range(len(val_dataset))):
        img_depth, img_seg, gt, valid = val_dataset[val_id]
        img_depth = img_depth[None].cuda()
        img_seg = img_seg[None].cuda()
 
        # pred = infer()
 
        flow_pr = model(img_depth) # infer?
 
        metrics.update(compute_metrics(gt, flow_pr))
 
    if round_vals:
        def r(m): return round(m, round_precision)
    else:
        def r(m): return m
    metrics = {k: r(v) for k, v in metrics.get_value().items()}
   
    return metrics
 
@torch.no_grad()
def validate_raw_kitti_for_depth_anything(model, round_vals=False, round_precision=3):
    """ Peform validation using the KITTI-2015 (train) split """
    model.eval()
    splits_dir = os.path.join(os.path.dirname(__file__), "splits")

    filenames = readlines(os.path.join(splits_dir, 'eigen', "test_files.txt"))
    h,w = 350, 1218
    dataset = dataset_raw_kitti.KITTIRAWDataset('../../datasets/kitti_data/', filenames,
                                           h, w,
                                           [0], 4, is_train=False)
    dataloader = DataLoader(dataset, 1, shuffle=False, num_workers=int(os.environ.get('SLURM_CPUS_PER_TASK', 6))-2,
                                pin_memory=True, drop_last=False)
    metrics = RunningAverageDict()
    num = 0
    for data in tqdm((dataloader)):
        input_color = data[("color", 0, 0)].cuda()
 
        # pred = infer()
        pred_disp = model(input_color)
        # pred_disp = infer(model, input_color)
        gt = data["depth_gt"].cuda()

        # print(f'{gt.shape, pred_disp.shape}')
        metric = compute_metrics(gt, pred_disp)
        metrics.update(metric)


        '''Inference 결과 저장 코드'''
        outdir = './DepthAnything_inference_result'
        if metric['a1']>=0.9:
            flow_pr = pred_disp.squeeze()
            flow_pr = (flow_pr - flow_pr.min()) / (flow_pr.max() - flow_pr.min()) * 255.0
            flow_pr = flow_pr.cpu().numpy().astype(np.uint8)
            flow_pr = cv2.applyColorMap(flow_pr, cv2.COLORMAP_INFERNO)
            
            if metric['a1']>=0.95:
                cv2.imwrite(os.path.join(outdir, '###AsymKD_Feas_'+str(num) + '_depth.png'), flow_pr)
            else:
                cv2.imwrite(os.path.join(outdir, 'AsymKD_Feas_'+str(num) + '_depth.png'), flow_pr)

        outdir = './DepthAnything_inference_input'
        if metric['a1']>=0.9:
            image1 = input_color
            input_image = (image1 - image1.min()) / (image1.max() - image1.min()) * 255.0
            input_image = input_image.cpu().numpy().astype(np.uint8)
            input_image = input_image[0].transpose(1, 2, 0)
            input_image = cv2.cvtColor(input_image, cv2.COLOR_RGB2BGR)
            if metric['a1']>=0.95:
                cv2.imwrite(os.path.join(outdir, '###AsymKD_Feas_'+str(num) + '_input.png'), input_image)
            else:
                cv2.imwrite(os.path.join(outdir, 'AsymKD_Feas_'+str(num) + '_input.png'), input_image)
        num += 1
 
    if round_vals:
        def r(m): return round(m, round_precision)
    else:
        def r(m): return m
    metrics = {k: r(v) for k, v in metrics.get_value().items()}
   
    return metrics
 
@torch.no_grad()
def validate_raw_kitti(model, seg_any_predictor: SamPredictor, round_vals=False, round_precision=3):
    """ Peform validation using the KITTI-2015 (train) split """
    model.eval()
    splits_dir = os.path.join(os.path.dirname(__file__), "splits")

    filenames = readlines(os.path.join(splits_dir, 'eigen', "test_files.txt"))
    h,w = 350, 1218
    dataset = dataset_raw_kitti.KITTIRAWDataset('../../datasets/kitti_data/', filenames,
                                           h, w,
                                           [0], 4,seg_any_predictor = seg_any_predictor, is_train=False)
    dataloader = DataLoader(dataset, 1, shuffle=False, num_workers=int(os.environ.get('SLURM_CPUS_PER_TASK', 6))-2,
                                drop_last=False)
    metrics = RunningAverageDict()
    num = 0
    for data in tqdm(dataloader):
        input_color = data[("color", 0, 0)].cuda()
        input_color_seg = data[("color_seg", 0, 0)].cuda()
 
        # pred = infer()
        pred_disp = model(input_color,input_color_seg)
        # pred_disp = infer(model, input_color)
        gt = data["depth_gt"].cuda()

        # print(f'{gt.shape, pred_disp.shape}')
        metric = compute_metrics(gt, pred_disp)
        metrics.update(metric)
        
        '''Inference 결과 저장 코드'''
        outdir = './AsymKD_inference_result'
        if metric['a1']>=0.9:
            flow_pr = pred_disp.squeeze()
            flow_pr = (flow_pr - flow_pr.min()) / (flow_pr.max() - flow_pr.min()) * 255.0
            flow_pr = flow_pr.cpu().numpy().astype(np.uint8)
            flow_pr = cv2.applyColorMap(flow_pr, cv2.COLORMAP_INFERNO)
            
            if metric['a1']>=0.95:
                cv2.imwrite(os.path.join(outdir, '###AsymKD_Feas_'+str(num) + '_depth.png'), flow_pr)
            else:
                cv2.imwrite(os.path.join(outdir, 'AsymKD_Feas_'+str(num) + '_depth.png'), flow_pr)

        outdir = './AsymKD_inference_input'
        if metric['a1']>=0.9:
            image1 = input_color
            input_image = (image1 - image1.min()) / (image1.max() - image1.min()) * 255.0
            input_image = input_image.cpu().numpy().astype(np.uint8)
            input_image = input_image[0].transpose(1, 2, 0)
            input_image = cv2.cvtColor(input_image, cv2.COLOR_RGB2BGR)
            if metric['a1']>=0.95:
                cv2.imwrite(os.path.join(outdir, '###AsymKD_Feas_'+str(num) + '_input.png'), input_image)
            else:
                cv2.imwrite(os.path.join(outdir, 'AsymKD_Feas_'+str(num) + '_input.png'), input_image)
        num += 1
 
    if round_vals:
        def r(m): return round(m, round_precision)
    else:
        def r(m): return m
    metrics = {k: r(v) for k, v in metrics.get_value().items()}
   
    return metrics
 
 
if __name__ == '__main__':
    torch.multiprocessing.set_start_method('spawn')
    DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'
    checkpoint = "sam_vit_b_01ec64.pth"
    # checkpoint = "sam_vit_l_0b3195.pth"
    model_type = "vit_b"
    segment_anything = sam_model_registry[model_type](checkpoint=checkpoint).to(DEVICE).eval()
    segment_anything_predictor = SamPredictor(segment_anything)
 
    # '''Depth Anything model load'''
    # encoder = 'vitl' # can also be 'vitb' or 'vitl'
    # model = DepthAnything.from_pretrained('LiheYoung/depth_anything_{}14'.format(encoder)).to(DEVICE).eval()
    # results = validate_raw_kitti_for_depth_anything(model)
    # print(f'Depth Anything evaluate result : {results}')    
    # print(f'#######{encoder} Depth Anything evaluate result#############')    
    # for key in results.keys():
    #     print(f'{key} : {round(results[key], 3)}')
   
    '''AsymKD model load'''
    for child in segment_anything.children():
            ImageEncoderViT = child
            break
    model = AsymKD_DepthAnything_Infer(ImageEncoderViT = ImageEncoderViT).to(DEVICE)
    restore_ckpt = 'checkpoints_new_loss_less_smooth/92000_AsymKD_new_loss.pth'
    # restore_ckpt = 'checkpoints/74994_epoch_AsymKD.pth'
    if restore_ckpt is not None:
        # assert restore_ckpt.endswith(".pth")
        logging.info("Loading checkpoint...")
        checkpoint = torch.load(restore_ckpt, map_location=DEVICE)
        model__state_dict = model.state_dict()
        new_state_dict = {}
        for k, v in checkpoint.items():
            # 키 매핑 규칙을 정의
            new_key = k.replace('module.', '')  # 'module.'를 제거
            if new_key in model__state_dict:
                new_state_dict[new_key] = v
 
        model__state_dict.update(new_state_dict)
        model.load_state_dict(model__state_dict)
    print(new_state_dict)
    model.to(DEVICE)
    model.eval()
    AsymKD_metric = validate_raw_kitti(model,segment_anything_predictor)
    print(f'AsymKD {restore_ckpt} evaluate result : {AsymKD_metric}')    
    print(f'#######AsymKD {restore_ckpt} diff evaluate result#############')  
    for key in AsymKD_metric.keys():
        print(f'{key} : {round(AsymKD_metric[key], 3)}')
        # print(f'diff {key} : {round(Depth_Any_metric[key]-AsymKD_metric[key], 3)}')
 
    Depth_Any_metric = {'a1': 0.8796647734923237, 'a2': 0.9656859071253321, 'a3': 0.9860573899123259, 'abs_rel': 0.11549764806391348, 'rmse': 4.7324441392589325, 'log_10': 0.049063832267926426, 'rmse_log': 0.1806166836860961, 'silog': 17.612650733573325, 'sq_rel': 0.8737658289350687}
    print(f'#######AsymKD {restore_ckpt} evaluate result#############')    
    for key in AsymKD_metric.keys():
        # print(f'{key} : {round(AsymKD_metric[key], 3)}')
        print(f'diff {key} : {round(Depth_Any_metric[key]-AsymKD_metric[key], 3)}')
