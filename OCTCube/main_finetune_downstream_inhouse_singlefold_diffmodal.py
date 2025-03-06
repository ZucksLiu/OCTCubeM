# Copyright (c) Zixuan Liu et al, OCTCubeM group
# All rights reserved.

# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.

# Revised by Zixuan Zucks Liu @University of Washington

# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
# Partly revised by YZ @UCL&Moorfields
# --------------------------------------------------------

import os
import time
import math
import json
import argparse
import datetime
import numpy as np

from pathlib import Path

import timm
from timm.models.layers import trunc_normal_
from timm.data.mixup import Mixup
from timm.loss import LabelSmoothingCrossEntropy, SoftTargetCrossEntropy

import torch
import torch.backends.cudnn as cudnn
from sklearn.model_selection import KFold
from torch.utils.tensorboard import SummaryWriter


import util.misc as misc
import util.lr_decay as lrd
from util.datasets import build_dataset
from util.datasets import build_transform, load_patient_list

from util.misc import NativeScalerWithGradNormCount as NativeScaler

from util.pos_embed import interpolate_pos_embed, interpolate_temporal_pos_embed
from util.WeightedLabelSmoothingCrossEntropy import WeightedLabelSmoothingCrossEntropy

from util.PatientDataset import TransformableSubset, PatientDataset3D, PatientDatasetCenter2D
from util.PatientDataset_inhouse import PatientDatasetCenter2D_inhouse, PatientDataset3D_inhouse, create_3d_transforms

from engine_finetune import train_one_epoch, evaluate, init_csv_writer

# RETFound-center
import models_vit
import models_vit_flash_attn

# RETFound-all
import models_vit_3dhead
import models_vit_3dhead_flash_attn

# OCTCube
import models_vit_st
import models_vit_st_joint
import models_vit_st_flash_attn
import models_vit_st_joint_flash_attn
import models_vit_st_flash_attn_nodrop
import models_vit_st_flash_attn_slivit

from util.MedMNISTDataset3D import MedMNISTDataset3D
from util.USDataset3D import USDataset3D
from util.misc_slivit import init_out_dir, save_options, setup_slivit_logger, assert_input_is_valid, setup_dataloaders


home_directory = os.getenv('HOME')

def setup(rank, world_size):
    dist.init_process_group("nccl", rank=rank, world_size=world_size)

def cleanup():
    dist.destroy_process_group()

def get_args_parser():
    parser = argparse.ArgumentParser('MAE fine-tuning for image classification', add_help=False)
    parser.add_argument('--regression_loss_name', default='l1loss', choices=['l1loss', 'custom_l1loss', 'custom_l1_l2_loss', 'huber_loss'], help='reg loss')
    # slivit experiment parameters
    parser.add_argument('--slivit_exp', default=False, action='store_true', help='Use slivit experiment')
    parser.add_argument('--slivit_dataset', default='us3d', type=str, choices=['us3d', 'ct3d'], help='slivit dataset')
    parser.add_argument('--slivit_out_dir', default='./slivit_output/', type=str, help='slivit output directory')
    parser.add_argument('--slivit_meta', default=None, type=str, help='slivit metadata file')
    parser.add_argument('--slivit_medmnist_root', default=home_directory + 'OCTCubeM/OCTCube/assets/medmnist_data/', type=str, help='slivit medmnist root directory')
    parser.add_argument('--slivit_slices', default=60, type=int, help='slivit slices')
    parser.add_argument('--slivit_label', default='EF_b', type=lambda x: x.split(','), help='slivit label')
    parser.add_argument('--slivit_split_col', default='split', type=str, help='slivit split column')
    parser.add_argument('--slivit_test_meta', default=None, type=str, help='slivit test metadata file')
    parser.add_argument('--slivit_pid_col', default='pid', type=str, help='slivit patient ID column')
    parser.add_argument('--slivit_path_col', type=str, default='path', help='Volume paths column name in the metadata CSV.')
    parser.add_argument('--slivit_img_suffix', type=str, default='.avi', help='File suffix to filter images (e.g., tiff, png).')
    parser.add_argument('--slivit_sparsing_method', type=str, default='eq', choices=['eq', 'mid', 'custom'],
                    help='Method for standardizing 3D data when there are different slice counts.')
    parser.add_argument('--slivit_us_auxi_reg', default=False, action='store_true', help='Use auxiliary regression for us3d reg')
    parser.add_argument('--slivit_3_channels', default=False, action='store_true', help='Use 3 channels for us3d')
    parser.add_argument('--slivit_w1', default=0.5, type=float, help='Weight for the auxiliary regression loss')
    parser.add_argument('--slivit_convert_vol', default=False, action='store_true', help='Convert volume to 3D volume')
    parser.add_argument('--slivit_vit_depth_num', default=5, type=int, help='Slice Vision Transformer number of layers')

    # newly added arguments
    parser.add_argument('--normalize_dataset', default=False, action='store_true', help='normalize dataset, used for baseline2 model')
    parser.add_argument('--return_bal_acc', default=False, action='store_true', help='return balanced accuracy')
    parser.add_argument('--downsample_normal', default=False, action='store_true', help='downsample normal cases')
    parser.add_argument('--downsample_normal_factor', default=10, type=int, help='downsample normal cases by a factor')
    parser.add_argument('--same_3_frames', default=False, action='store_true', help='use the same 3 frames to mock 1 frame for 3D spatio-temporal model')
    parser.add_argument('--use_high_res_patch_embed', default=False, action='store_true', help='use high resolution patch embedding')
    parser.add_argument('--variable_joint', default=False, action='store_true', help='use variable joint attention')
    parser.set_defaults(variable_joint=False) # We disable variable joint attention by default
    parser.add_argument('--high_res_num_frames', default=30, type=int, help='number of high resolution frames')
    parser.add_argument('--high_res_input_size', default=512, type=int, help='high resolution patch size')
    parser.add_argument('--focal_loss', default=False, action='store_true', help='use focal loss')
    parser.add_argument('--load_non_flash_attn_to_flash_attn', default=False, action='store_true', help='use focal loss')
    parser.add_argument('--always_test', default=False, action='store_true', help='always run test if specified')
    parser.add_argument('--use_cls_idx', default=None, nargs='+', type=int, help='List of integers')
    parser.add_argument('--linear_probe', default=False, action='store_true', help='linear probe')
    parser.add_argument('--load_teacher_model', default=False, action='store_true', help='load teacher model')
    parser.add_argument('--save_model_every', type=int, default=1, help='Save model every k epochs (if enabled)')
    parser.add_argument('--not_print_logits', default=False, action='store_true', help='not print logits')
    parser.add_argument('--not_save_figs', default=False, action='store_true', help='not save figures')
    parser.add_argument('--multi_task_idx', type=misc.str_to_int_list, default=None, help='List of integers')

    # mae_st parameters
    parser.add_argument("--t_patch_size", default=3, type=int)
    parser.add_argument("--num_frames", default=60, type=int)
    parser.add_argument("--pad_to_num_frames", default=False, action="store_true")
    parser.add_argument("--sep_pos_embed", action="store_true")
    parser.set_defaults(sep_pos_embed=True)
    parser.add_argument("--cls_embed", action="store_true")
    parser.set_defaults(cls_embed=True)
    parser.add_argument("--transform_type", default="frame_2D", type=str, choices=["frame_2D", "monai_3D"])
    parser.add_argument("--color_mode", default="rgb", type=str, choices=["rgb", "gray"])
    parser.add_argument("--smaller_temporal_crop", default='interp', type=str, choices=['interp', 'crop'], help='interpolation type for temporal position embedding')

    # Dataset parameters
    parser.add_argument('--data_path', default=home_directory + '/Ophthal/', type=str, help='dataset path')
    parser.add_argument('--patient_dataset', default=False, action='store_true', help='Use patient dataset')
    parser.add_argument('--patient_dataset_type', default='Center2D', type=str, choices=['3D', 'Center2D', 'Center2D_flash_attn',  '3D_flash_attn', '3D_st', '3D_st_joint', '3D_st_flash_attn', '3D_st_joint_flash_attn', '3D_st_flash_attn_nodrop', '3D_st_flash_attn_slivit'], help='patient dataset type')
    parser.add_argument('--patient_idx_loc', default=2, type=int, help='patient index location in the image filename, e.g., 2 for amd_oct_2_1.png')
    parser.add_argument('--nb_classes', default=2, type=int, help='number of the classification types')

    parser.add_argument('--disease', default='AMD', type=str, choices=['AMD', 'DME', 'POG', 'ODR', 'PM', 'CRO', 'RN', 'VD'], help='Disease type for the dataset (only for binary_cls task_mode)')
    parser.add_argument('--task_mode', default='binary_cls', type=str, choices=['binary_cls', 'multi_cls', 'multi_label', 'multi_task', 'multi_task_default', 'regression'], help='Task mode for the dataset')
    parser.add_argument('--val_metric', default='AUPRC', type=str, choices=['AUC', 'ACC', 'AUPRC'], help='Validation metric for early stopping')

    parser.add_argument('--save_model', default=False, action='store_true', help='save model')
    parser.add_argument('--single_fold', default=False, action='store_true', help='few shot learning')
    parser.add_argument('--split_path', default=None, type=str, help='split path storing the train/val/test split of patient files')
    parser.add_argument('--patient_id_list_dir', default='multi_cls_expr_10x_0315/', type=str, help='patient id list dir')
    parser.add_argument('--enable_early_stop', default=False, action='store_true', help='enable early stop')
    parser.add_argument('--early_stop_patience', default=10, type=int, help='early stop patience')

    # K_fold cross validation
    parser.add_argument('--k_fold', default=False, action='store_true', help='Use K-fold cross validation')
    parser.add_argument('--k_folds', default=5, type=int, help='number of folds for K-fold cross validation')


    parser.add_argument('--batch_size', default=64, type=int,
                        help='Batch size per GPU (effective batch size is batch_size * accum_iter * # gpus')
    parser.add_argument('--epochs', default=50, type=int)
    parser.add_argument('--accum_iter', default=1, type=int,
                        help='Accumulate gradient iterations (for increasing the effective batch size under memory constraints)')

    # Model parameters
    parser.add_argument('--model', default='vit_large_patch16', type=str, metavar='MODEL',
                        help='Name of model to train')

    parser.add_argument('--input_size', default=224, type=int,
                        help='images input size')

    parser.add_argument('--drop_path', type=float, default=0.1, metavar='PCT',
                        help='Drop path rate (default: 0.1)')

    # Optimizer parameters
    parser.add_argument('--clip_grad', type=float, default=None, metavar='NORM',
                        help='Clip gradient norm (default: None, no clipping)')
    parser.add_argument('--weight_decay', type=float, default=0.05,
                        help='weight decay (default: 0.05)')

    parser.add_argument('--lr', type=float, default=None, metavar='LR',
                        help='learning rate (absolute lr)')
    parser.add_argument('--blr', type=float, default=1e-3, metavar='LR',
                        help='base learning rate: absolute_lr = base_lr * total_batch_size / 256')
    parser.add_argument('--layer_decay', type=float, default=0.75,
                        help='layer-wise lr decay from ELECTRA/BEiT')

    parser.add_argument('--min_lr', type=float, default=1e-6, metavar='LR',
                        help='lower lr bound for cyclic schedulers that hit 0')

    parser.add_argument('--warmup_epochs', type=int, default=2, metavar='N',
                        help='epochs to warmup LR')

    # Augmentation parameters
    parser.add_argument('--color_jitter', type=float, default=None, metavar='PCT',
                        help='Color jitter factor (enabled only when not using Auto/RandAug)')
    parser.add_argument('--aa', type=str, default='rand-m9-mstd0.5-inc1', metavar='NAME',
                        help='Use AutoAugment policy. "v0" or "original". " + "(default: rand-m9-mstd0.5-inc1)'),
    parser.add_argument('--smoothing', type=float, default=0.1,
                        help='Label smoothing (default: 0.1)')

    # * Random Erase params
    parser.add_argument('--reprob', type=float, default=0.25, metavar='PCT',
                        help='Random erase prob (default: 0.25)')
    parser.add_argument('--remode', type=str, default='pixel',
                        help='Random erase mode (default: "pixel")')
    parser.add_argument('--recount', type=int, default=1,
                        help='Random erase count (default: 1)')
    parser.add_argument('--resplit', action='store_true', default=False,
                        help='Do not random erase first (clean) augmentation split')

    # * Mixup params
    parser.add_argument('--mixup', type=float, default=0,
                        help='mixup alpha, mixup enabled if > 0.')
    parser.add_argument('--cutmix', type=float, default=0,
                        help='cutmix alpha, cutmix enabled if > 0.')
    parser.add_argument('--cutmix_minmax', type=float, nargs='+', default=None,
                        help='cutmix min/max ratio, overrides alpha and enables cutmix if set (default: None)')
    parser.add_argument('--mixup_prob', type=float, default=1.0,
                        help='Probability of performing mixup or cutmix when either/both is enabled')
    parser.add_argument('--mixup_switch_prob', type=float, default=0.5,
                        help='Probability of switching to cutmix when both mixup and cutmix enabled')
    parser.add_argument('--mixup_mode', type=str, default='batch',
                        help='How to apply mixup/cutmix params. Per "batch", "pair", or "elem"')

    # * Finetuning params
    parser.add_argument('--finetune', default='',type=str,
                        help='finetune from checkpoint')
    parser.add_argument('--few_shot', default=False, action='store_true',
                        help='finetune from checkpoint')
    parser.add_argument('--task', default='./finetune_umn/',type=str,
                        help='finetune from checkpoint')
    parser.add_argument('--global_pool', action='store_true')
    parser.set_defaults(global_pool=True)
    parser.add_argument('--cls_token', action='store_false', dest='global_pool',
                        help='Use class token instead of global pool for classification')


    parser.add_argument('--output_dir', default='./outputs_ft/',
                        help='path where to save, empty for no saving')
    parser.add_argument('--log_dir', default='./output_dir',
                        help='path where to tensorboard log')
    parser.add_argument('--device', default='cuda',
                        help='device to use for training / testing')
    parser.add_argument('--seed', default=0, type=int)
    parser.add_argument('--resume', default='',
                        help='resume from checkpoint')

    parser.add_argument('--start_epoch', default=0, type=int, metavar='N',
                        help='start epoch')
    parser.add_argument('--eval', action='store_true',
                        help='Perform evaluation only')
    parser.add_argument('--dist_eval', action='store_true', default=False,
                        help='Enabling distributed evaluation (recommended during training for faster monitor')
    parser.add_argument('--num_workers', default=8, type=int)
    parser.add_argument('--pin_mem', action='store_true',
                        help='Pin CPU memory in DataLoader for more efficient (sometimes) transfer to GPU.')
    parser.add_argument('--no_pin_mem', action='store_false', dest='pin_mem')
    parser.set_defaults(pin_mem=True)

    # distributed training parameters
    parser.add_argument('--world_size', default=1, type=int,
                        help='number of distributed processes')
    parser.add_argument('--local_rank', default=-1, type=int)
    parser.add_argument('--dist_on_itp', action='store_true')
    parser.add_argument('--dist_url', default='env://',
                        help='url used to set up distributed training')

    return parser


def main(args):
    misc.init_distributed_mode(args)

    print('job dir: {}'.format(os.path.dirname(os.path.realpath(__file__))))
    print("{}".format(args).replace(', ', ',\n'))

    if args.slivit_exp:
        init_out_dir(args)
        slivit_logger = setup_slivit_logger(args)

        args.output_dir = args.slivit_out_dir
        args.task = args.slivit_out_dir + '/'
        os.makedirs(args.output_dir, exist_ok=True)
        save_options(args, slivit_logger)
        assert_input_is_valid(args, slivit_logger)

        dls, test_loader = setup_dataloaders(args, slivit_logger)
        print('args.output_dir:', args.output_dir)
        print('args.task:', args.task)

    # Save args to a json file
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        with open(os.path.join(args.output_dir, 'args.json'), 'w') as f:
            stored_json = vars(args)
            for key, value in stored_json.items():
                try:
                    # dump key and value to json
                    # and try to make it single dict-like
                    print(json.dumps(value))

                except:
                    print(f"Failed to save {key} to json")
                    stored_json[key] = str(value)
                    continue
            json.dump(stored_json, f, indent=2)
        print(f"Saved args to {args.output_dir}/args.json")


    device = torch.device(args.device)

    # fix the seed for reproducibility
    seed = args.seed + misc.get_rank()
    torch.manual_seed(seed)
    np.random.seed(seed)
    if not args.return_bal_acc:
        val_bal_acc = None
        test_bal_acc = None

    cudnn.benchmark = True

    if args.variable_joint:
        high_res_num_frames = args.high_res_num_frames
        train_transform_high_res, val_transform_high_res = create_3d_transforms(input_size=args.high_res_input_size, num_frames=args.high_res_num_frames, RandFlipd_prob=0.5, RandRotate90d_prob=0.5, normalize=False)
    else:
        high_res_num_frames = None
        train_transform_high_res = None
        val_transform_high_res = None

    if not args.patient_dataset:
        if not args.slivit_exp:
            dataset_train = build_dataset(is_train='train', args=args)
            dataset_val = build_dataset(is_train='val', args=args)
            dataset_test = build_dataset(is_train='test', args=args)
            assert args.k_fold is False
        else:
            data_loader_train = dls[0]
            data_loader_val = dls[1]
            data_loader_test = test_loader
    else:
        if args.transform_type == 'frame_2D':
            train_transform = build_transform(is_train='train', args=args)
            val_transform = build_transform(is_train='val', args=args)
        elif args.transform_type == 'monai_3D':
            train_transform, val_transform = create_3d_transforms(**vars(args))
        if args.patient_dataset_type == '3D' or args.patient_dataset_type == '3D_st' or args.patient_dataset_type == '3D_st_joint' or args.patient_dataset_type.startswith('3D'):
            dataset_for_Kfold = PatientDataset3D_inhouse(root_dir=args.data_path, transform=None, disease=args.disease, dataset_mode='frame', mode=args.color_mode, task_mode=args.task_mode, iterate_mode='visit', downsample_width=True, patient_id_list_dir=args.patient_id_list_dir, pad_to_num_frames=args.pad_to_num_frames, padding_num_frames=args.num_frames, transform_type=args.transform_type, downsample_normal=args.downsample_normal, same_3_frames=args.same_3_frames, return_both_res_image=args.variable_joint, high_res_transform=None, high_res_num_frames=args.high_res_num_frames, downsample_normal_factor=args.downsample_normal_factor, multi_task_idx=args.multi_task_idx)
        elif args.patient_dataset_type == 'Center2D' or args.patient_dataset_type == 'Center2D_flash_attn':
            dataset_for_Kfold = PatientDatasetCenter2D_inhouse(root_dir=args.data_path, transform=None, disease=args.disease, dataset_mode='frame', mode='rgb', task_mode=args.task_mode, iterate_mode='visit', downsample_width=True, patient_id_list_dir=args.patient_id_list_dir, downsample_normal=args.downsample_normal, multi_task_idx=args.multi_task_idx)

        if args.k_fold:
            # Assuming KFold setup is external, and args.fold indicates the current fold
            kf = KFold(n_splits=args.k_folds, shuffle=True, random_state=args.seed)

            patient_mapping_visit_indices = sorted(list(dataset_for_Kfold.mapping_patient2visit.keys()))
            rng = np.random.RandomState(args.seed)
            patient_mapping_visit_indices = rng.permutation(patient_mapping_visit_indices)
            folds = list(kf.split(patient_mapping_visit_indices))


        elif args.single_fold:
            train_pat_id = load_patient_list(args.split_path, split='train', name_suffix='_pat_list.txt')
            val_pat_id = load_patient_list(args.split_path, split='val', name_suffix='_pat_list.txt')
            test_pat_id = load_patient_list(args.split_path, split='test', name_suffix='_pat_list.txt')
            included_patient = list(dataset_for_Kfold.patients.keys())

            filtered_train_pat_id = sorted(list(set(train_pat_id) & set(included_patient)))
            filtered_val_pat_id = sorted(list(set(val_pat_id) & set(included_patient)))
            filtered_test_pat_id = sorted(list(set(test_pat_id) & set(included_patient)))

            train_pat_indices = dataset_for_Kfold.get_visit_idx(filtered_train_pat_id)
            val_pat_indices = dataset_for_Kfold.get_visit_idx(filtered_val_pat_id)
            test_pat_indices = dataset_for_Kfold.get_visit_idx(filtered_test_pat_id)


            # OCTCube or RETFound-all
            if args.patient_dataset_type == '3D' or args.patient_dataset_type == '3D_st' or args.patient_dataset_type == '3D_st_joint' or args.patient_dataset_type.startswith('3D'):
                if args.few_shot:
                    if args.downsample_normal:
                        adjusted_indices = dataset_for_Kfold.adjusted_indices
                        val_indices = sorted(list(set(val_pat_indices) & set(adjusted_indices)))
                    else:
                        val_indices = val_pat_indices
                    dataset_train = TransformableSubset(dataset_for_Kfold, val_indices)
                    dataset_val = TransformableSubset(dataset_for_Kfold, train_pat_indices)
                else:
                    if args.downsample_normal:
                        adjusted_indices = dataset_for_Kfold.adjusted_indices
                        print('len(adjusted_indices):', len(adjusted_indices))
                        print('len(train_pat_indices):', len(train_pat_indices))
                        train_indices = sorted(list(set(train_pat_indices) & set(adjusted_indices)))
                        print('len(train_indices) after:', len(train_indices))
                    else:
                        train_indices = train_pat_indices
                    dataset_train = TransformableSubset(dataset_for_Kfold, train_indices)
                    dataset_val = TransformableSubset(dataset_for_Kfold, val_pat_indices)
                dataset_test = TransformableSubset(dataset_for_Kfold, test_pat_indices)
                dataset_train.update_dataset_transform(train_transform)
                if args.variable_joint:
                    dataset_train.update_dataset_transform_high_res(train_transform_high_res)

            # RETFound-center
            elif args.patient_dataset_type == 'Center2D' or args.patient_dataset_type == 'Center2D_flash_attn':
                if args.few_shot:
                    if args.downsample_normal:
                        adjusted_indices = dataset_for_Kfold.adjusted_indices
                        val_indices = sorted(list(set(val_pat_indices) & set(adjusted_indices)))
                    else:
                        val_indices = val_pat_indices
                    dataset_train = TransformableSubset(dataset_for_Kfold, val_indices, transform=train_transform)
                    dataset_val = TransformableSubset(dataset_for_Kfold, train_pat_indices, transform=val_transform)
                else:
                    if args.downsample_normal:
                        adjusted_indices = dataset_for_Kfold.adjusted_indices
                        print('len(adjusted_indices):', len(adjusted_indices))
                        print('len(train_pat_indices):', len(train_pat_indices))
                        train_indices = sorted(list(set(train_pat_indices) & set(adjusted_indices)))
                        print('len(train_indices) after:', len(train_indices))
                    else:
                        train_indices = train_pat_indices
                    dataset_train = TransformableSubset(dataset_for_Kfold, train_indices, transform=train_transform)
                    dataset_val = TransformableSubset(dataset_for_Kfold, val_pat_indices, transform=val_transform)
                dataset_test = TransformableSubset(dataset_for_Kfold, test_pat_indices, transform=val_transform)

    num_tasks = misc.get_world_size()
    global_rank = misc.get_rank()
    if args.k_fold and args.patient_dataset: # K-fold cross validation, if your dataset is large, we recommend to use single_fold and run multiple times to avoid CUDA failure
        fold_results = []
        fold_results_test = []
        print(f"Start K-fold cross validation for {args.k_folds} folds")
        for fold in range(args.k_folds):
            print(f"Fold {fold}")
            idx_train_pat_id, idx_val_pat_id = folds[fold]
            train_pat_id, val_pat_id = [patient_mapping_visit_indices[idx] for idx in idx_train_pat_id], [patient_mapping_visit_indices[idx] for idx in idx_val_pat_id]

            train_indices = dataset_for_Kfold.get_visit_idx(train_pat_id)
            val_indices = dataset_for_Kfold.get_visit_idx(val_pat_id)

            if args.patient_dataset_type == '3D' or args.patient_dataset_type == '3D_st' or args.patient_dataset_type == '3D_st_joint' or args.patient_dataset_type.startswith('3D'):
                if args.few_shot:
                    dataset_train = TransformableSubset(dataset_for_Kfold, val_indices)
                    dataset_val = TransformableSubset(dataset_for_Kfold, train_indices)
                else:
                    dataset_train = TransformableSubset(dataset_for_Kfold, train_indices)
                    dataset_val = TransformableSubset(dataset_for_Kfold, val_indices)
                dataset_train.update_dataset_transform(train_transform)
                if args.variable_joint:
                    dataset_train.update_dataset_transform_high_res(train_transform_high_res)

            elif args.patient_dataset_type == 'Center2D' or args.patient_dataset_type == 'Center2D_flash_attn':
                if args.few_shot:
                    dataset_train = TransformableSubset(dataset_for_Kfold, val_indices, transform=train_transform)
                    dataset_val = TransformableSubset(dataset_for_Kfold, train_indices, transform=val_transform)
                else:
                    dataset_train = TransformableSubset(dataset_for_Kfold, train_indices, transform=train_transform)
                    dataset_val = TransformableSubset(dataset_for_Kfold, val_indices, transform=val_transform)
            dataset_test = dataset_val
            sampler_train = torch.utils.data.DistributedSampler(
                dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True
            )

            print("Sampler_train = %s" % str(sampler_train))
            if args.dist_eval:
                if len(dataset_val) % num_tasks != 0:
                    print('Warning: Enabling distributed evaluation with an eval dataset not divisible by process number. '
                        'This will slightly alter validation results as extra duplicate entries are added to achieve '
                        'equal num of samples per-process.')
                sampler_val = torch.utils.data.DistributedSampler(
                    dataset_val, num_replicas=num_tasks, rank=global_rank, shuffle=True)  # shuffle=True to reduce monitor bias
                sampler_test = sampler_val
            else:
                sampler_val = torch.utils.data.SequentialSampler(dataset_val)
                sampler_test = sampler_val

            if global_rank == 0 and args.log_dir is not None and not args.eval:
                os.makedirs(args.log_dir, exist_ok=True)
                log_writer = SummaryWriter(log_dir=args.log_dir+args.task)
            else:
                log_writer = None

            data_loader_train = torch.utils.data.DataLoader(
                dataset_train, sampler=sampler_train,
                batch_size=args.batch_size,
                num_workers=args.num_workers,
                pin_memory=args.pin_mem,
                drop_last=True,
            )

            data_loader_val = torch.utils.data.DataLoader(
                dataset_val, sampler=sampler_val,
                batch_size=args.batch_size,
                num_workers=args.num_workers,
                pin_memory=args.pin_mem,
                drop_last=False
            )

            data_loader_test = torch.utils.data.DataLoader(
                dataset_test, sampler=sampler_test,
                batch_size=args.batch_size,
                num_workers=args.num_workers,
                pin_memory=args.pin_mem,
                drop_last=False
            )

            mixup_fn = None
            mixup_active = args.mixup > 0 or args.cutmix > 0. or args.cutmix_minmax is not None
            if mixup_active:
                print("Mixup is activated!")
                mixup_fn = Mixup(
                    mixup_alpha=args.mixup, cutmix_alpha=args.cutmix, cutmix_minmax=args.cutmix_minmax,
                    prob=args.mixup_prob, switch_prob=args.mixup_switch_prob, mode=args.mixup_mode,
                    label_smoothing=args.smoothing, num_classes=args.nb_classes)
            if args.patient_dataset_type == '3D':
                model = models_vit_3dhead.__dict__[args.model](
                    img_size=args.input_size,
                    num_classes=args.nb_classes,
                    drop_path_rate=args.drop_path,
                    global_pool=args.global_pool,
                )
            elif args.patient_dataset_type == 'Center2D':
                model = models_vit.__dict__[args.model](
                    img_size=args.input_size,
                    num_classes=args.nb_classes,
                    drop_path_rate=args.drop_path,
                    global_pool=args.global_pool,
                )
            elif args.patient_dataset_type == 'Center2D_flash_attn':
                model = models_vit_flash_attn.__dict__[args.model](
                    img_size=args.input_size,
                    num_classes=args.nb_classes,
                    drop_path_rate=args.drop_path,
                    global_pool=args.global_pool,
                )
            elif args.patient_dataset_type == '3D_st':
                model = models_vit_st.__dict__[args.model](
                    img_size=args.input_size,
                    num_classes=args.nb_classes,
                    drop_path_rate=args.drop_path,
                    global_pool=args.global_pool,
                    t_patch_size=args.t_patch_size,
                    num_frames=args.num_frames,
                    sep_pos_embed=args.sep_pos_embed,
                    cls_embed=args.cls_embed,
                )
            elif args.patient_dataset_type == '3D_st_joint':
                model = models_vit_st_joint_flash_attn.__dict__[args.model](
                    img_size=args.input_size,
                    num_classes=args.nb_classes,
                    drop_path_rate=args.drop_path,
                    global_pool=args.global_pool,
                    t_patch_size=args.t_patch_size,
                    num_frames=args.num_frames,
                    sep_pos_embed=args.sep_pos_embed,
                    cls_embed=args.cls_embed,
                    transform_type=args.transform_type,
                    color_mode=args.color_mode,
                    smaller_temporal_crop=args.smaller_temporal_crop,
                    use_high_res_patch_embed=args.use_high_res_patch_embed,
                )
            elif args.patient_dataset_type == '3D_st_flash_attn':
                print('Use 3D spatio-temporal model w/ flash attention')
                model = models_vit_st_flash_attn.__dict__[args.model](
                        num_frames=args.num_frames,
                        t_patch_size=args.t_patch_size,
                        img_size=args.input_size,
                        num_classes=args.nb_classes,
                        drop_path_rate=args.drop_path,
                        global_pool=args.global_pool,
                        sep_pos_embed=args.sep_pos_embed,
                        cls_embed=args.cls_embed,
                        use_flash_attention=True,
                        dropout=args.dropout,
                    )
            elif args.patient_dataset_type == '3D_st_flash_attn_nodrop':
                print('Use 3D spatio-temporal model w/ flash attention and no dropout')
                model = models_vit_st_flash_attn_nodrop.__dict__[args.model](
                        num_frames=args.num_frames,
                        t_patch_size=args.t_patch_size,
                        img_size=args.input_size,
                        num_classes=args.nb_classes,
                        drop_path_rate=args.drop_path,
                        global_pool=args.global_pool,
                        sep_pos_embed=args.sep_pos_embed,
                        cls_embed=args.cls_embed,
                        use_flash_attention=True
                    )

            if args.finetune and not args.eval:
                checkpoint = torch.load(args.finetune, map_location='cpu')

                print("Load pre-trained checkpoint from: %s" % args.finetune)
                print(args.load_teacher_model)

                if args.load_teacher_model:
                    assert 'teacher_model' in checkpoint, 'No teacher model in checkpoint'

                    checkpoint_model = checkpoint['teacher_model']
                else:
                    checkpoint_model = checkpoint['model']
                state_dict = model.state_dict()
                for k in ['head.weight', 'head.bias']:
                    if k in checkpoint_model and checkpoint_model[k].shape != state_dict[k].shape:
                        print(f"Removing key {k} from pretrained checkpoint")
                        del checkpoint_model[k]

                # interpolate position embedding
                interpolate_pos_embed(model, checkpoint_model)

                # load pre-trained model
                msg = model.load_state_dict(checkpoint_model, strict=False)
                print(msg)
                print(msg.missing_keys)
                if args.global_pool:
                    if args.patient_dataset_type == '3D':
                        assert set(msg.missing_keys) == {'fc_aggregate_cls.weight', 'fc_aggregate_cls.bias',
                        'aggregate_cls_norm.weight', 'aggregate_cls_norm.bias',
                        'head.weight', 'head.bias', 'fc_norm.weight', 'fc_norm.bias'}
                    elif args.patient_dataset_type == '3D_st_flash_attn_nodrop':
                        print('Goin right way')
                        assert set(msg.missing_keys) == {'fc_aggregate_cls.weight', 'fc_aggregate_cls.bias',
                        'aggregate_cls_norm.weight', 'aggregate_cls_norm.bias',
                        'head.weight', 'head.bias'}
                    elif args.patient_dataset_type == 'Center2D' or args.patient_dataset_type == 'Center2D_flash_attn':
                        assert set(msg.missing_keys) == {'head.weight', 'head.bias', 'fc_norm.weight', 'fc_norm.bias'}
                    elif args.patient_dataset_type == '3D_st' or args.patient_dataset_type == '3D_st_joint' or args.patient_dataset_type == '3D_st_flash_attn':
                        assert set(msg.missing_keys) == {'head.weight', 'head.bias'}
                else:
                    assert set(msg.missing_keys) == {'head.weight', 'head.bias'}

                # manually initialize fc layer
                trunc_normal_(model.head.weight, std=2e-5)
            model.to(device)

            model_without_ddp = model
            n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)

            print("Model = %s" % str(model_without_ddp))
            print('number of params (M): %.2f' % (n_parameters / 1.e6))

            eff_batch_size = args.batch_size * args.accum_iter * misc.get_world_size()

            if args.lr is None:  # only base_lr is specified
                args.lr = args.blr * eff_batch_size / 256

            print("base lr: %.2e" % (args.lr * 256 / eff_batch_size))
            print("actual lr: %.2e" % args.lr)

            print("accumulate grad iterations: %d" % args.accum_iter)
            print("effective batch size: %d" % eff_batch_size)
            if args.distributed:
                model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
                model_without_ddp = model.module

            # build optimizer with layer-wise lr decay (lrd)
            param_groups = lrd.param_groups_lrd(model_without_ddp, args.weight_decay,
                no_weight_decay_list=model_without_ddp.no_weight_decay(),
                layer_decay=args.layer_decay
            )
            optimizer = torch.optim.AdamW(param_groups, lr=args.lr)
            loss_scaler = NativeScaler()

            if mixup_fn is not None:
                # smoothing is handled with mixup label transform
                criterion = SoftTargetCrossEntropy()
            elif args.task_mode == 'multi_label':
                criterion = torch.nn.BCEWithLogitsLoss()
            elif args.task_mode == 'multi_task' or args.task_mode == 'multi_task_default':
                print( 'Use multi-task loss')
                criterion = WeightedLabelSmoothingCrossEntropy(smoothing=args.smoothing)
            elif args.task_mode == 'regression':
                criterion = torch.nn.L1Loss()
            elif args.smoothing > 0.:
                criterion = LabelSmoothingCrossEntropy(smoothing=args.smoothing)
            else:
                criterion = torch.nn.CrossEntropyLoss()

            print("criterion = %s" % str(criterion))

            misc.load_model(args=args, model_without_ddp=model_without_ddp, optimizer=optimizer, loss_scaler=loss_scaler)

            if args.eval:
                test_mode = f'test_fold_{fold}'
                init_csv_writer(args.task, mode=test_mode)
                test_stats, auc_roc, auc_pr = evaluate(data_loader_test, model, device, args.task, epoch=0, mode=test_mode, num_class=args.nb_classes, criterion=criterion, task_mode=args.task_mode, disease_list=None, return_bal_acc=args.return_bal_acc, args=args)
                if args.return_bal_acc:
                    test_auc_pr, test_bal_acc = auc_pr
                exit(0)

            print(f"Start training for {args.epochs} epochs")
            start_time = time.time()

            # initialize metric values tracking the best model
            max_accuracy = 0.0
            max_auc = 0.0
            max_auc_pr = 0.0
            max_epoch = 0
            max_accuracy_test = 0.0
            max_auc_test = 0.0
            max_auc_pr_test = 0.0
            max_epoch_test = 0

            max_bal_acc = 0.0
            max_bal_acc_test = 0.0

            val_mode = f'val_fold_{fold}'
            if args.task_mode == 'binary_cls':
                init_csv_writer(args.task, mode=val_mode)

            for epoch in range(args.start_epoch, args.epochs):
                if args.distributed:
                    data_loader_train.sampler.set_epoch(epoch)
                train_stats = train_one_epoch(
                    model, criterion, data_loader_train,
                    optimizer, device, epoch, loss_scaler,
                    args.clip_grad, mixup_fn,
                    log_writer=log_writer,
                    args=args
                )
                if train_stats is None:
                    # downscale the learning rate by 2
                    for param_group in optimizer.param_groups:
                        param_group['lr'] /= 2
                    print(f"Downscale the learning rate to {param_group['lr']}")
                if args.patient_dataset_type == '3D' or args.patient_dataset_type == '3D_st' or args.patient_dataset_type == '3D_st_joint' or args.patient_dataset_type.startswith('3D'):
                    dataset_train.remove_dataset_transform()
                    dataset_val.update_dataset_transform(val_transform)
                    if args.variable_joint:
                        dataset_train.remove_dataset_transform_high_res()
                        dataset_val.update_dataset_transform_high_res(val_transform_high_res)

                if args.task_mode == 'multi_label' or args.task_mode == 'multi_task' or args.task_mode == 'multi_task_default':
                    disease_list = dataset_for_Kfold.idx_to_disease
                else:
                    disease_list = None

                try:
                    val_stats, val_auc_roc, val_auc_pr = evaluate(data_loader_val, model, device, args.task, epoch, mode=val_mode, num_class=args.nb_classes, criterion=criterion, task_mode=args.task_mode, disease_list=disease_list, return_bal_acc=args.return_bal_acc, args=args)
                    if args.return_bal_acc:
                        val_auc_pr, val_bal_acc = val_auc_pr
                except ValueError as e:
                    print(e)
                    print('break')
                    print(f'break at {epoch}', file=open(os.path.join(args.output_dir, f"auc_fold_{fold}.txt"), mode="a"))
                    break

                max_flag = False
                if args.val_metric == 'AUC':
                    print('Use AUC as the validation metric')
                    if max_auc <= val_auc_roc:
                        max_auc = val_auc_roc
                        if max_auc < val_auc_roc:
                            max_epoch = epoch
                            max_flag = True
                        elif max_accuracy <= val_stats['acc1']:
                            max_accuracy = val_stats['acc1']
                            max_epoch = epoch
                            max_flag = True
                        elif max_auc_pr <= val_auc_pr:
                            max_auc_pr = val_auc_pr
                            max_epoch = epoch
                            max_flag = True
                elif args.val_metric == 'AUPRC':
                    print('Use AUPRC as the validation metric')
                    if max_auc_pr <= val_auc_pr:
                        if max_auc_pr < val_auc_pr:
                            max_epoch = epoch
                            max_auc = val_auc_roc
                            max_accuracy = val_stats['acc1']
                            max_flag = True
                        max_auc_pr = val_auc_pr
                        if max_accuracy <= val_stats['acc1']:
                            max_accuracy = val_stats['acc1']
                            max_auc = val_auc_roc
                            max_epoch = epoch
                            max_flag = True
                        elif max_auc <= val_auc_roc:
                            max_auc = val_auc_roc
                            max_accuracy = val_stats['acc1']
                            max_epoch = epoch
                            max_flag = True

                        if val_bal_acc is not None:
                            max_bal_acc = val_bal_acc

                if max_flag is True:
                    print(f"Max AUC: {max_auc}, Max ACC: {max_accuracy}, Max AUCPR: {max_auc_pr}, Max Bal Acc: {max_bal_acc}, at epoch {epoch}")
                    print(f"Max AUC: {max_auc}, Max ACC: {max_accuracy}, Max AUCPR: {max_auc_pr}, Max Bal Acc: {max_bal_acc}, at epoch {epoch}", file=open(os.path.join(args.output_dir, f"auc_fold_{fold}.txt"), mode="a"))
                    if args.output_dir and args.save_model and (epoch + 1) % args.save_model_every == 0:
                        misc.save_model(
                            args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                            loss_scaler=loss_scaler, epoch=epoch)

                if max_flag or epoch == (args.epochs - 1):
                    test_mode = f'test_fold_{fold}'
                    init_csv_writer(args.task, mode=test_mode)
                    try:
                        test_stats, test_auc_roc, test_auc_pr = evaluate(data_loader_test, model, device, args.task, epoch, mode=test_mode, num_class=args.nb_classes, criterion=criterion, task_mode=args.task_mode, disease_list=disease_list, return_bal_acc=args.return_bal_acc, args=args)
                        if args.return_bal_acc:
                            test_auc_pr, test_bal_acc = test_auc_pr
                    except ValueError as e:
                        print(e)
                        print('break')
                        break
                    max_flag_test = False
                    if args.val_metric == 'AUC':
                        print('Use AUC as the validation metric')
                        if max_auc_test <= test_auc_roc:
                            max_auc_test = test_auc_roc
                            if max_auc_test < test_auc_roc:
                                max_epoch_test = epoch
                                max_flag_test = True
                            elif max_accuracy_test <= test_stats['acc1']:
                                max_accuracy_test = test_stats['acc1']
                                max_epoch_test = epoch
                                max_flag_test = True
                            elif max_auc_pr_test <= test_auc_pr:
                                max_auc_pr_test = test_auc_pr
                                max_epoch_test = epoch
                                max_flag_test = True
                    elif args.val_metric == 'AUPRC':
                        print('Use AUPRC as the validation metric')
                        if max_auc_pr_test <= test_auc_pr:

                            if max_auc_pr_test < test_auc_pr:
                                max_epoch_test = epoch
                                max_auc_test = test_auc_roc
                                max_accuracy_test = test_stats['acc1']
                                max_flag_test = True
                            max_auc_pr_test = test_auc_pr
                            if max_accuracy_test <= test_stats['acc1']:
                                max_accuracy_test = test_stats['acc1']
                                max_auc_test = test_auc_roc
                                max_epoch_test = epoch
                                max_flag_test = True
                            elif max_auc_test <= test_auc_roc:
                                max_auc_test = test_auc_roc
                                max_accuracy_test = test_stats['acc1']
                                max_epoch_test = epoch
                                max_flag_test = True
                            if args.return_bal_acc:
                                max_bal_acc_test = test_bal_acc
                    if max_flag_test is True:
                        print(f"Max AUC: {max_auc_test}, Max ACC: {max_accuracy_test}, Max AUCPR: {max_auc_pr_test}, Max Bal Acc: {max_bal_acc_test}, at epoch {epoch}")
                        print(f"Max AUC: {max_auc_test}, Max ACC: {max_accuracy_test}, Max AUCPR: {max_auc_pr_test}, Max Bal Acc: {max_bal_acc_test}, at epoch {epoch}", file=open(os.path.join(args.output_dir, f"auc_test_fold_{fold}.txt"), mode="a"))


                if log_writer is not None:
                    log_writer.add_scalar('perf/val_acc1', val_stats['acc1'], epoch)
                    log_writer.add_scalar('perf/val_auc', val_auc_roc, epoch)
                    log_writer.add_scalar('perf/val_auc_pr', val_auc_pr, epoch)
                    log_writer.add_scalar('perf/val_loss', val_stats['loss'], epoch)
                    if args.return_bal_acc and val_bal_acc is not None:
                        log_writer.add_scalar('perf/val_bal_acc', val_bal_acc, epoch)
                if train_stats is not None:
                    log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                                    'epoch': epoch,
                                    'n_parameters': n_parameters,
                                    'max_val_auc': max_auc,
                                    'max_val_acc': max_accuracy,
                                    'max_val_auc_pr': max_auc_pr,
                                    'max_val_epoch': max_epoch,
                                    'max_val_bal_acc': max_bal_acc}

                if args.output_dir and misc.is_main_process():
                    if log_writer is not None:
                        log_writer.flush()
                    with open(os.path.join(args.output_dir, f"log_fold_{fold}.txt"), mode="a") as f:
                        f.write(json.dumps(log_stats) + "\n")

                if args.patient_dataset_type == '3D' or args.patient_dataset_type == '3D_st' or args.patient_dataset_type == '3D_st_joint' or args.patient_dataset_type.startswith('3D'):
                    dataset_val.remove_dataset_transform()
                    dataset_train.update_dataset_transform(train_transform)
                    if args.variable_joint:
                        dataset_val.remove_dataset_transform_high_res()
                        dataset_train.update_dataset_transform_high_res(train_transform_high_res)

            total_time = time.time() - start_time
            total_time_str = str(datetime.timedelta(seconds=int(total_time)))
            print('Training time {}'.format(total_time_str))
            print('Training time {}'.format(total_time_str), file=open(os.path.join(args.output_dir, f"time_fold_{fold}.txt"), mode="a"))
            if args.return_bal_acc:
                fold_results.append((max_auc, max_accuracy, max_auc_pr, max_bal_acc))
                fold_results_test.append((max_auc_test, max_accuracy_test, max_auc_pr_test, max_bal_acc_test))
            else:
                fold_results.append((max_auc, max_accuracy, max_auc_pr))
                fold_results_test.append((max_auc_test, max_accuracy_test, max_auc_pr_test))

        # Calculate average AUC and accuracy and std
        fold_results = np.array(fold_results)
        fold_results_mean = np.mean(fold_results, axis=0)
        fold_results_std = np.std(fold_results, axis=0)

        print(f"Fold results: {fold_results}\nMean: {fold_results_mean}\nStd: {fold_results_std}")
        print(f"Fold results: {fold_results}\nMean: {fold_results_mean}\nStd: {fold_results_std}",
            file=open(os.path.join(args.output_dir, "fold_results.txt"), mode="a"))

        # Calculate average AUC and accuracy and std
        fold_results_test = np.array(fold_results_test)
        fold_results_mean_test = np.mean(fold_results_test, axis=0)
        fold_results_std_test = np.std(fold_results_test, axis=0)

        print(f"Fold results: {fold_results_test}\nMean: {fold_results_mean_test}\nStd: {fold_results_std_test}")
        print(f"Fold results: {fold_results_test}\nMean: {fold_results_mean_test}\nStd: {fold_results_std_test}",
            file=open(os.path.join(args.output_dir, "fold_results_test.txt"), mode="a"))

    else: # Single fold, more common use case

        assert args.single_fold is True
        fold_results = []
        fold_results_test = []
        fold_results_test_for_best_val = []

        if global_rank == 0 and args.log_dir is not None and not args.eval:
            os.makedirs(args.log_dir, exist_ok=True)
            log_writer = SummaryWriter(log_dir=args.log_dir+args.task)
        else:
            log_writer = None

        if not args.slivit_exp:
            print(f"Start train val test for {len(train_pat_indices)} train, {len(val_pat_indices)} val, {len(test_pat_indices)} test")

            sampler_train = torch.utils.data.DistributedSampler(
                dataset_train, num_replicas=num_tasks, rank=global_rank, shuffle=True
            )

            print("Sampler_train = %s" % str(sampler_train))
            if args.dist_eval:
                if len(dataset_val) % num_tasks != 0:
                    print('Warning: Enabling distributed evaluation with an eval dataset not divisible by process number. '
                        'This will slightly alter validation results as extra duplicate entries are added to achieve '
                        'equal num of samples per-process.')
                sampler_val = torch.utils.data.DistributedSampler(
                    dataset_val, num_replicas=num_tasks, rank=global_rank, shuffle=True)  # shuffle=True to reduce monitor bias

            else:
                sampler_val = torch.utils.data.SequentialSampler(dataset_val)

            if args.dist_eval:
                if len(dataset_test) % num_tasks != 0:
                    print('Warning: Enabling distributed evaluation with an eval dataset not divisible by process number. '
                        'This will slightly alter validation results as extra duplicate entries are added to achieve '
                        'equal num of samples per-process.')
                sampler_test = torch.utils.data.DistributedSampler(
                    dataset_test, num_replicas=num_tasks, rank=global_rank, shuffle=True)  # shuffle=True to reduce monitor bias
            else:
                sampler_test = torch.utils.data.SequentialSampler(dataset_test)


            data_loader_train = torch.utils.data.DataLoader(
                dataset_train, sampler=sampler_train,
                batch_size=args.batch_size,
                num_workers=args.num_workers,
                pin_memory=args.pin_mem,
                drop_last=True,
            )

            data_loader_val = torch.utils.data.DataLoader(
                dataset_val, sampler=sampler_val,
                batch_size=args.batch_size,
                num_workers=args.num_workers,
                pin_memory=args.pin_mem,
                drop_last=False
            )

            data_loader_test = torch.utils.data.DataLoader(
                dataset_test, sampler=sampler_test,
                batch_size=args.batch_size,
                num_workers=args.num_workers,
                pin_memory=args.pin_mem,
                drop_last=False
            )

        print('Length of train, val, test:', len(data_loader_train), len(data_loader_val), len(data_loader_test))

        mixup_fn = None
        mixup_active = args.mixup > 0 or args.cutmix > 0. or args.cutmix_minmax is not None
        if mixup_active:
            print("Mixup is activated!")
            mixup_fn = Mixup(
                mixup_alpha=args.mixup, cutmix_alpha=args.cutmix, cutmix_minmax=args.cutmix_minmax,
                prob=args.mixup_prob, switch_prob=args.mixup_switch_prob, mode=args.mixup_mode,
                label_smoothing=args.smoothing, num_classes=args.nb_classes)
        if args.patient_dataset_type == '3D':
            model = models_vit_3dhead.__dict__[args.model](
                img_size=args.input_size,
                num_classes=args.nb_classes,
                drop_path_rate=args.drop_path,
                global_pool=args.global_pool,
            )
        elif args.patient_dataset_type == 'Center2D':
            model = models_vit.__dict__[args.model](
                img_size=args.input_size,
                num_classes=args.nb_classes,
                drop_path_rate=args.drop_path,
                global_pool=args.global_pool,
            )
        elif args.patient_dataset_type == 'Center2D_flash_attn':
            model = models_vit_flash_attn.__dict__[args.model](
                img_size=args.input_size,
                num_classes=args.nb_classes,
                drop_path_rate=args.drop_path,
                global_pool=args.global_pool,
            )
        elif args.patient_dataset_type == '3D_flash_attn':
            print('Use 3D flash attn model')
            model = models_vit_3dhead_flash_attn.__dict__[args.model](
                img_size=args.input_size,
                num_classes=args.nb_classes,
                drop_path_rate=args.drop_path,
                global_pool=args.global_pool,
            )
        elif args.patient_dataset_type == '3D_st':
            print('Use 3D spatio-temporal model')
            model = models_vit_st.__dict__[args.model](
                    num_frames=args.num_frames,
                    t_patch_size=args.t_patch_size,
                    img_size=args.input_size,
                    num_classes=args.nb_classes,
                    drop_path_rate=args.drop_path,
                    global_pool=args.global_pool,
                    sep_pos_embed=args.sep_pos_embed,
                    cls_embed=args.cls_embed,
                )
        elif args.patient_dataset_type == '3D_st_joint':
            model = models_vit_st_joint.__dict__[args.model](
                    img_size=args.input_size,
                    num_classes=args.nb_classes,
                    drop_path_rate=args.drop_path,
                    global_pool=args.global_pool,
                    t_patch_size=args.t_patch_size,
                    num_frames=args.num_frames,
                    sep_pos_embed=args.sep_pos_embed,
                    cls_embed=args.cls_embed,
                    transform_type=args.transform_type,
                    color_mode=args.color_mode,
                    smaller_temporal_crop=args.smaller_temporal_crop,
                    use_high_res_patch_embed=args.use_high_res_patch_embed,
                )
        elif args.patient_dataset_type == '3D_st_flash_attn':
            print('Use 3D spatio-temporal model w/ flash attention')
            model = models_vit_st_flash_attn.__dict__[args.model](
                    num_frames=args.num_frames,
                    t_patch_size=args.t_patch_size,
                    img_size=args.input_size,
                    num_classes=args.nb_classes,
                    drop_path_rate=args.drop_path,
                    global_pool=args.global_pool,
                    sep_pos_embed=args.sep_pos_embed,
                    cls_embed=args.cls_embed,
                    use_flash_attention=True
                )
        elif args.patient_dataset_type == '3D_st_joint_flash_attn':
            model = models_vit_st_joint_flash_attn.__dict__[args.model](
                    img_size=args.input_size,
                    num_classes=args.nb_classes,
                    drop_path_rate=args.drop_path,
                    global_pool=args.global_pool,
                    t_patch_size=args.t_patch_size,
                    num_frames=args.num_frames,
                    sep_pos_embed=args.sep_pos_embed,
                    cls_embed=args.cls_embed,
                    transform_type=args.transform_type,
                    color_mode=args.color_mode,
                    smaller_temporal_crop=args.smaller_temporal_crop,
                    use_high_res_patch_embed=args.use_high_res_patch_embed,
                    use_flash_attention=True
                )
        elif args.patient_dataset_type == '3D_st_flash_attn_nodrop':
            model = models_vit_st_flash_attn_nodrop.__dict__[args.model](
                    num_frames=args.num_frames,
                    t_patch_size=args.t_patch_size,
                    image_size=args.input_size,
                    num_classes=args.nb_classes,
                    drop_path_rate=args.drop_path,
                    global_pool=args.global_pool,
                    sep_pos_embed=args.sep_pos_embed,
                    cls_embed=args.cls_embed,
                    use_flash_attention=True
                    )

        elif args.patient_dataset_type == '3D_st_flash_attn_slivit':
            model = models_vit_st_flash_attn_slivit.__dict__[args.model](
                    num_frames=args.num_frames,
                    t_patch_size=args.t_patch_size,
                    img_size=args.input_size,
                    num_classes=args.nb_classes,
                    drop_path_rate=args.drop_path,
                    global_pool=args.global_pool,
                    sep_pos_embed=args.sep_pos_embed,
                    cls_embed=args.cls_embed,
                    use_flash_attention=True,
                    slivit_depth_num=args.slivit_vit_depth_num,
                )

        if args.finetune and not args.eval:
            checkpoint = torch.load(args.finetune, map_location='cpu')

            print("Load pre-trained checkpoint from: %s" % args.finetune)

            if args.load_teacher_model:
                assert 'teacher_model' in checkpoint, 'No teacher model in checkpoint'

                checkpoint_model = checkpoint['teacher_model']
            else:
                checkpoint_model = checkpoint['model']

            state_dict = model.state_dict()
            print(checkpoint_model.keys())
            for k in ['head.weight', 'head.bias']:
                if k in checkpoint_model and checkpoint_model[k].shape != state_dict[k].shape:
                    print(f"Removing key {k} from pretrained checkpoint")
                    print(k, checkpoint_model[k].shape, state_dict[k].shape)
                    del checkpoint_model[k]

            # interpolate position embedding
            if args.sep_pos_embed and (args.patient_dataset_type == '3D_st' or args.patient_dataset_type == '3D_st_joint' or args.patient_dataset_type.startswith('3D_st')): # OCTCube
                interpolate_pos_embed(model, checkpoint_model)
                interpolate_temporal_pos_embed(model, checkpoint_model, smaller_interpolate_type=args.smaller_temporal_crop)
            else:
                interpolate_pos_embed(model, checkpoint_model)


            # load pre-trained model
            if args.load_non_flash_attn_to_flash_attn:
                msg = model.load_state_dict_to_backbone(checkpoint["model"])
            else:
                msg = model.load_state_dict(checkpoint_model, strict=False)
            print(msg)
            print(msg.missing_keys)
            if args.global_pool:
                if args.patient_dataset_type == '3D' or args.patient_dataset_type == '3D_flash_attn':
                    assert set(msg.missing_keys) == {'fc_aggregate_cls.weight', 'fc_aggregate_cls.bias',
                    'aggregate_cls_norm.weight', 'aggregate_cls_norm.bias',
                    'head.weight', 'head.bias', 'fc_norm.weight', 'fc_norm.bias'}
                elif args.patient_dataset_type == '3D_st_flash_attn_nodrop':
                    print('Goin right way')
                    assert set(msg.missing_keys) == {'fc_aggregate_cls.weight', 'fc_aggregate_cls.bias',
                    'aggregate_cls_norm.weight', 'aggregate_cls_norm.bias',
                    'head.weight', 'head.bias'}
                elif args.patient_dataset_type == 'Center2D' or args.patient_dataset_type == 'Center2D_flash_attn':
                    assert set(msg.missing_keys) == {'head.weight', 'head.bias', 'fc_norm.weight', 'fc_norm.bias'}
                # assert set(msg.missing_keys) == {'head.weight', 'head.bias', 'fc_norm.weight', 'fc_norm.bias'}i
                elif args.patient_dataset_type == '3D_st' or args.patient_dataset_type == '3D_st_joint' or args.patient_dataset_type == '3D_st_flash_attn' or args.patient_dataset_type == '3D_st_joint_flash_attn':
                    assert set(msg.missing_keys) == {'head.weight', 'head.bias'}
            else:
                if args.patient_dataset_type == '3D_st_flash_attn_nodrop' or args.patient_dataset_type == '3D_flash_attn':
                    print('Goin right way')
                    assert set(msg.missing_keys) == {'fc_aggregate_cls.weight', 'fc_aggregate_cls.bias',
                    'aggregate_cls_norm.weight', 'aggregate_cls_norm.bias',
                    'head.weight', 'head.bias'}
                else:
                    assert set(msg.missing_keys) == {'head.weight', 'head.bias'}

            if args.patient_dataset_type != '3D_st_flash_attn_slivit':
                trunc_normal_(model.head.weight, std=2e-5)

        if args.linear_probe:
            for _, p in model.named_parameters():
                p.requires_grad = False
            for _, p in model.head.named_parameters():
                p.requires_grad = True
        model.to(device)

        model_without_ddp = model
        n_parameters = sum(p.numel() for p in model.parameters() if p.requires_grad)

        print("Model = %s" % str(model_without_ddp))
        print('number of params (M): %.2f' % (n_parameters / 1.e6))

        eff_batch_size = args.batch_size * args.accum_iter * misc.get_world_size()

        if args.lr is None:  # only base_lr is specified
            args.lr = args.blr * eff_batch_size / 256

        print("base lr: %.2e" % (args.lr * 256 / eff_batch_size))
        print("actual lr: %.2e" % args.lr)

        print("accumulate grad iterations: %d" % args.accum_iter)
        print("effective batch size: %d" % eff_batch_size)

        if args.distributed:
            model = torch.nn.parallel.DistributedDataParallel(model, device_ids=[args.gpu])
            model_without_ddp = model.module

        # build optimizer with layer-wise lr decay (lrd)
        param_groups = lrd.param_groups_lrd(model_without_ddp, args.weight_decay,
            no_weight_decay_list=model_without_ddp.no_weight_decay(),
            layer_decay=args.layer_decay
        )
        optimizer = torch.optim.AdamW(param_groups, lr=args.lr)
        loss_scaler = NativeScaler()

        if mixup_fn is not None:
            # smoothing is handled with mixup label transform
            criterion = SoftTargetCrossEntropy()
        elif args.task_mode == 'multi_label':
            if args.focal_loss:
                print("Use focal loss")
                from util.focal_loss import FocalLoss2d
                criterion = FocalLoss2d(gamma=2.0)

            else:
                criterion = torch.nn.BCEWithLogitsLoss()
        elif args.task_mode == 'regression':
            def elastic_net_loss(y_pred, y_true, alpha=0.5, l1_ratio=0.5):
                # print(y_true.dtype, y_pred.dtype)
                mse_loss = torch.nn.MSELoss()
                l1_loss = torch.nn.L1Loss()
                loss = mse_loss(y_pred, y_true)
                l1_reg = l1_loss(y_pred, y_true)
                loss = loss + alpha * l1_ratio * l1_reg + 0.5 * alpha * (1 - l1_ratio) * torch.norm(y_pred, p=2) / (1 + alpha * l1_ratio + 1e-8)
                return loss

            def huber_loss(y_pred, y_true, delta=1.0, alpha=0.33, w1=0.1, w2=0.1):
                residual = y_true - y_pred
                abs_residual = torch.abs(residual)

                # Huber loss: Quadratic for small residuals, linear for large residuals
                loss = torch.where(
                    abs_residual <= delta,
                    residual ** 2,  # Quadratic region
                    2 * delta * abs_residual - delta ** 2 + alpha * (abs_residual - delta)  # Linear region with alpha adjustment
                )

                # Regularization terms
                reg_term = torch.where(
                    y_pred > 0,
                    w1 * torch.norm(y_pred, p=2),  # 2-norm regularization for y_pred > 0
                    w2 * torch.norm(y_pred, p=1)   # 1-norm regularization for y_pred < 0
                )

                # Combine loss and regularizer
                total_loss = loss + reg_term

                return torch.mean(total_loss)

            def custom_l1_l2_loss(y_pred, y_true, w1=0.1, w2=0.1):
                mse_loss = torch.nn.MSELoss()
                l1_loss = torch.nn.L1Loss()
                loss = mse_loss(y_pred, y_true)
                l1_reg = l1_loss(y_pred, y_true)
                loss = 0.5 * loss + 0.5 * l1_reg
                reg_term = torch.where(
                    y_pred > 0,
                    w1 * torch.norm(y_pred, p=2),  # 2-norm regularization for y_pred > 0
                    w2 * torch.norm(y_pred, p=1)   # 1-norm regularization for y_pred < 0
                )
                total_loss = loss + torch.mean(reg_term)

                return loss

            def custom_l1_loss(y_pred, y_true, w1=0.1, w2=0.1):
                l1_loss = torch.nn.L1Loss()
                l1_reg = l1_loss(y_pred, y_true)
                reg_term = torch.where(
                    y_pred > 0,
                    w1 * torch.norm(y_pred, p=2),  # 2-norm regularization for y_pred > 0
                    w2 * torch.norm(y_pred, p=1)   # 1-norm regularization for y_pred < 0
                )
                total_loss = l1_reg + torch.mean(reg_term)

                return total_loss

            def weighted_l1_loss(y_pred, y_true, w1=args.slivit_w1):
                l1_loss = torch.nn.L1Loss()
                if len(y_true.shape) > 1:
                    l1_reg = l1_loss(y_pred[:, 0], y_true[:, 0])
                    l1_reg_auxiliary = l1_loss(y_pred[:, 1:], y_true[:, 1:])
                    l1_reg = (l1_reg + w1 * l1_reg_auxiliary) / (1 + w1 * (y_true.shape[1] - 1))
                else:
                    l1_reg = l1_loss(y_pred, y_true)
                return l1_reg


            # criterion = elastic_net_loss
            if args.regression_loss_name == 'custom_l1loss':
                criterion = custom_l1_loss
            elif args.regression_loss_name == 'huber_loss':
                criterion = huber_loss
            elif args.regression_loss_name == 'custom_l1_l2_loss':
                criterion = custom_l1_l2_loss
            elif args.regression_loss_name == 'l1loss':
                # criterion = torch.nn.L1Loss()
                criterion = weighted_l1_loss

        elif args.task_mode == 'multi_task' or args.task_mode == 'multi_task_default':
            print( 'Use multi-task loss')
            criterion = WeightedLabelSmoothingCrossEntropy(smoothing=args.smoothing)
        elif args.smoothing > 0.:
            criterion = LabelSmoothingCrossEntropy(smoothing=args.smoothing)
        else:
            criterion = torch.nn.CrossEntropyLoss()

        print("criterion = %s" % str(criterion))

        misc.load_model(args=args, model_without_ddp=model_without_ddp, optimizer=optimizer, loss_scaler=loss_scaler)

        if args.eval:
            test_mode = f'test_singlefold'
            init_csv_writer(args.task, mode=test_mode)
            test_stats, auc_roc, auc_pr = evaluate(data_loader_test, model, device, args.task, epoch=0, mode=test_mode, num_class=args.nb_classes, criterion=criterion, task_mode=args.task_mode, disease_list=None, return_bal_acc=args.return_bal_acc, args=args)
            if args.return_bal_acc:
                auc_pr, test_bal_acc = auc_pr
            exit(0)

        print(f"Start training for {args.epochs} epochs")
        start_time = time.time()
        max_accuracy = -0.01
        max_auc = -0.01
        max_auc_pr = -0.01
        max_epoch = 0
        max_accuracy_test = -0.01
        max_auc_test = -0.01
        max_auc_pr_test = -0.01
        max_epoch_test = 0
        # storing the test metrics for the best val epoch
        cur_auc_test = -0.01
        cur_auc_pr_test = -0.01
        cur_acc_test = -0.01
        cur_epoch_test = 0
        val_mode = f'val_singlefold'
        test_mode = f'test_singlefold'

        max_bal_acc = -0.01
        max_bal_acc_test = -0.01
        cur_bal_acc_test = -0.01

        if args.task_mode == 'binary_cls':
            init_csv_writer(args.task, mode=val_mode)
            init_csv_writer(args.task, mode=test_mode)

        if args.enable_early_stop:
            early_stop_counter = 0

        for epoch in range(args.start_epoch, args.epochs):
            if args.distributed:
                data_loader_train.sampler.set_epoch(epoch)
            train_stats = train_one_epoch(
                model, criterion, data_loader_train,
                optimizer, device, epoch, loss_scaler,
                args.clip_grad, mixup_fn,
                log_writer=log_writer,
                args=args
            )

            if train_stats is None:
                # downscale the learning rate by 2
                for param_group in optimizer.param_groups:
                    param_group['lr'] /= 2
                    print(f"Downscale the learning rate to {param_group['lr']}")

            if args.patient_dataset and (args.patient_dataset_type == '3D' or args.patient_dataset_type == '3D_st' or args.patient_dataset_type == '3D_st_joint' or args.patient_dataset_type.startswith('3D')): # OCTCube or RETFound-all
                dataset_train.remove_dataset_transform()
                dataset_val.update_dataset_transform(val_transform)
                if args.variable_joint:
                    dataset_train.remove_dataset_transform_high_res()
                    dataset_val.update_dataset_transform_high_res(val_transform_high_res)

            if args.task_mode == 'multi_label' or args.task_mode == 'multi_task' or args.task_mode == 'multi_task_default':
                disease_list = dataset_for_Kfold.idx_to_disease
            else:
                disease_list = None

            try:
                val_returned_all_results = evaluate(data_loader_val, model, device, args.task, epoch, mode=val_mode, num_class=args.nb_classes, criterion=criterion, task_mode=args.task_mode, disease_list=disease_list, return_bal_acc=args.return_bal_acc, args=args)
                if args.task_mode == 'regression':
                    val_stats = val_returned_all_results
                    val_auc_pr = val_stats['r2']
                    val_auc_roc = val_stats['explained_variance']
                    if args.return_bal_acc:
                        val_bal_acc = val_stats['mse']
                else:
                    val_stats, val_auc_roc, val_auc_pr = val_returned_all_results
                    if args.return_bal_acc:
                        val_auc_pr, val_bal_acc = val_auc_pr
            except ValueError as e:
                print(e)
                print('break')
                print(f'break at {epoch}', file=open(os.path.join(args.output_dir, f"auc_singlefold.txt"), mode="a"))
                break

            max_flag = False
            if args.task_mode == 'regression':
                acc_mock_name = 'mae'
                print_metric_name_list = ['Explained Variance', 'MAE', 'R2', 'MSE']
            else:
                acc_mock_name = 'acc1'
                print_metric_name_list = ['AUC', 'ACC', 'AUCPR', 'Bal Acc']

            if args.val_metric == 'AUC':
                print('Use AUC as the validation metric')
                if max_auc <= val_auc_roc:
                    max_auc = val_auc_roc
                    if max_auc < val_auc_roc:
                        max_epoch = epoch
                        max_flag = True
                    elif max_accuracy <= val_stats[acc_mock_name]:
                        max_accuracy = val_stats[acc_mock_name]
                        max_epoch = epoch
                        max_flag = True
                    elif max_auc_pr <= val_auc_pr:
                        max_auc_pr = val_auc_pr
                        max_epoch = epoch
                        max_flag = True
            elif args.val_metric == 'AUPRC':
                print('Use AUPRC as the validation metric')
                if max_auc_pr <= val_auc_pr:
                    if max_auc_pr < val_auc_pr:
                        max_epoch = epoch
                        max_auc = val_auc_roc
                        max_accuracy = val_stats[acc_mock_name]
                        max_flag = True

                    max_auc_pr = val_auc_pr
                    if max_accuracy <= val_stats[acc_mock_name]:
                        max_accuracy = val_stats[acc_mock_name]
                        max_auc = val_auc_roc
                        max_epoch = epoch
                        max_flag = True
                    elif max_auc <= val_auc_roc:
                        max_auc = val_auc_roc
                        max_accuracy = val_stats[acc_mock_name]
                        max_epoch = epoch
                        max_flag = True

                    if val_bal_acc is not None:
                        max_bal_acc = val_bal_acc

            if max_flag is True:
                print(f"Max {print_metric_name_list[0]}: {max_auc:4f}, Max {print_metric_name_list[1]}: {max_accuracy:4f}, Max {print_metric_name_list[2]}: {max_auc_pr:4f}, Max {print_metric_name_list[3]}: {max_bal_acc:4f}, at epoch {epoch}")
                print(f"Max {print_metric_name_list[0]}: {max_auc:4f}, Max {print_metric_name_list[1]}: {max_accuracy:4f}, Max {print_metric_name_list[2]}: {max_auc_pr:4f}, Max {print_metric_name_list[3]}: {max_bal_acc:4f}, at epoch {epoch}", file=open(os.path.join(args.output_dir, f"auc_singlefold.txt"), mode="a"))

                if args.output_dir and args.save_model and (epoch + 1) % args.save_model_every == 0:
                    misc.save_model(
                        args=args, model=model, model_without_ddp=model_without_ddp, optimizer=optimizer,
                        loss_scaler=loss_scaler, epoch=epoch)

                if args.enable_early_stop:
                    early_stop_counter = 0

            if max_flag or epoch == (args.epochs - 1) or args.always_test:
                try:
                    test_returned_all_results = evaluate(data_loader_test, model, device, args.task, epoch, mode=test_mode, num_class=args.nb_classes, criterion=criterion, task_mode=args.task_mode, disease_list=disease_list, return_bal_acc=args.return_bal_acc, args=args)
                    if args.task_mode == 'regression':
                        test_stats = test_returned_all_results
                        test_auc_pr = test_stats['r2']
                        test_auc_roc = test_stats['explained_variance']
                        if args.return_bal_acc:
                            test_bal_acc = test_stats['mse']
                    else:
                        test_stats, test_auc_roc, test_auc_pr = test_returned_all_results
                        if args.return_bal_acc:
                            test_auc_pr, test_bal_acc = test_auc_pr
                except ValueError as e:
                    print(e)
                    print('break')
                    break
                max_flag_test = False
                if args.val_metric == 'AUC':
                    print('Test: Use AUC as the validation metric')
                    if max_auc_test <= test_auc_roc:
                        max_auc_test = test_auc_roc
                        if max_auc_test < test_auc_roc:
                            max_epoch_test = epoch
                            max_flag_test = True
                        elif max_accuracy_test <= test_stats[acc_mock_name]:
                            max_accuracy_test = test_stats[acc_mock_name]
                            max_epoch_test = epoch
                            max_flag_test = True
                        elif max_auc_pr_test <= test_auc_pr:
                            max_auc_pr_test = test_auc_pr
                            max_epoch_test = epoch
                            max_flag_test = True
                elif args.val_metric == 'AUPRC':
                    print('Test: Use AUPRC as the validation metric')
                    if max_auc_pr_test <= test_auc_pr:

                        if max_auc_pr_test < test_auc_pr:
                            max_epoch_test = epoch
                            max_auc_test = test_auc_roc
                            max_accuracy_test = test_stats[acc_mock_name]
                            max_flag_test = True
                        max_auc_pr_test = test_auc_pr
                        if max_accuracy_test <= test_stats[acc_mock_name]:
                            max_accuracy_test = test_stats[acc_mock_name]
                            max_auc_test = test_auc_roc
                            max_epoch_test = epoch
                            max_flag_test = True
                        elif max_auc_test <= test_auc_roc:
                            max_auc_test = test_auc_roc
                            max_accuracy_test = test_stats[acc_mock_name]
                            max_epoch_test = epoch
                            max_flag_test = True
                        if args.return_bal_acc:
                            max_bal_acc_test = test_bal_acc
                if max_flag_test is True:
                    print(f"Max {print_metric_name_list[0]}: {max_auc_test:4f}, Max {print_metric_name_list[1]}: {max_accuracy_test:4f}, Max {print_metric_name_list[2]}: {max_auc_pr_test:4f}, Max {print_metric_name_list[3]}: {max_bal_acc_test:4f}, at epoch {epoch}")
                    print(f"Max {print_metric_name_list[0]}: {max_auc_test:4f}, Max {print_metric_name_list[1]}: {max_accuracy_test:4f}, Max {print_metric_name_list[2]}: {max_auc_pr_test:4f}, Max {print_metric_name_list[3]}: {max_bal_acc_test:4f}, at epoch {epoch}", file=open(os.path.join(args.output_dir, f"auc_test_singlefold.txt"), mode="a"))

                # storing the test metrics for the best val epoch
                cur_auc_test = test_auc_roc
                cur_auc_pr_test = test_auc_pr
                cur_acc_test = test_stats[acc_mock_name]
                cur_epoch_test = epoch
                if test_bal_acc is not None:
                    cur_bal_acc_test = test_bal_acc


            if max_flag is not True and args.enable_early_stop:
                early_stop_counter += 1
                if early_stop_counter > args.early_stop_patience:
                    print(f"Early stop at epoch {epoch}")
                    print(f"Early stop at epoch {epoch}", file=open(os.path.join(args.output_dir, f"auc_singlefold.txt"), mode="a"))
                    break

            if log_writer is not None:
                log_writer.add_scalar('perf/val_acc1', val_stats[acc_mock_name], epoch)
                log_writer.add_scalar('perf/val_auc', val_auc_roc, epoch)
                log_writer.add_scalar('perf/val_auc_pr', val_auc_pr, epoch)
                log_writer.add_scalar('perf/val_loss', val_stats['loss'], epoch)
                if args.return_bal_acc and val_bal_acc is not None:
                    log_writer.add_scalar('perf/val_bal_acc', val_bal_acc, epoch)
            if train_stats is not None:
                log_stats = {**{f'train_{k}': v for k, v in train_stats.items()},
                                'epoch': epoch,
                                'n_parameters': n_parameters,
                                'max_val_auc': max_auc,
                                'max_val_acc': max_accuracy,
                                'max_val_auc_pr': max_auc_pr,
                                'max_val_epoch': max_epoch,
                                'max_val_bal_acc': max_bal_acc}


            if args.output_dir and misc.is_main_process():
                if log_writer is not None:
                    log_writer.flush()
                with open(os.path.join(args.output_dir, "log.txt"), mode="a") as f:
                    f.write(json.dumps(log_stats) + "\n")

            if args.patient_dataset and (args.patient_dataset_type == '3D' or args.patient_dataset_type == '3D_st' or args.patient_dataset_type == '3D_st_joint' or args.patient_dataset_type.startswith('3D')):
                dataset_val.remove_dataset_transform()
                dataset_train.update_dataset_transform(train_transform)
                if args.variable_joint:
                    dataset_val.remove_dataset_transform_high_res()
                    dataset_train.update_dataset_transform_high_res(train_transform_high_res)


            if args.downsample_normal:
                dataset_train.dataset.on_epoch_end()
                if args.few_shot:
                    adjusted_indices = dataset_for_Kfold.adjusted_indices
                    val_indices = sorted(list(set(val_pat_indices) & set(adjusted_indices)))
                    dataset_train.update_indices(val_indices)
                    data_loader_train.sampler.num_samples = math.ceil(len(dataset_train) / num_tasks)
                    data_loader_train.sampler.total_size = data_loader_train.sampler.num_samples * num_tasks

                else:
                    print('len(train_indices) before:', len(train_indices))
                    adjusted_indices = dataset_for_Kfold.adjusted_indices
                    print('len(adjusted_indices):', len(adjusted_indices))
                    print('len(train_pat_indices):', len(train_pat_indices))
                    train_indices = sorted(list(set(train_pat_indices) & set(adjusted_indices)))
                    print('len(train_indices) after:', len(train_indices))
                    dataset_train.update_indices(train_indices)
                    data_loader_train.sampler.num_samples = math.ceil(len(dataset_train) / num_tasks)
                    data_loader_train.sampler.total_size = data_loader_train.sampler.num_samples * num_tasks

        total_time = time.time() - start_time
        total_time_str = str(datetime.timedelta(seconds=int(total_time)))
        print('Training time {}'.format(total_time_str))
        print('Training time {}'.format(total_time_str), file=open(os.path.join(args.output_dir, "time.txt"), mode="a"))

        if args.return_bal_acc:
            fold_results.append((max_auc, max_accuracy, max_auc_pr, max_bal_acc))
            fold_results_test.append((max_auc_test, max_accuracy_test, max_auc_pr_test, max_bal_acc_test))
            fold_results_test_for_best_val.append((cur_auc_test, cur_acc_test, cur_auc_pr_test, cur_bal_acc_test))
        else:
            fold_results.append((max_auc, max_accuracy, max_auc_pr))
            fold_results_test.append((max_auc_test, max_accuracy_test, max_auc_pr_test))
            fold_results_test_for_best_val.append((cur_auc_test, cur_acc_test, cur_auc_pr_test))


        # Calculate average AUC and accuracy and std
        fold_results = np.array(fold_results)
        fold_results_mean = np.mean(fold_results, axis=0)
        fold_results_std = np.std(fold_results, axis=0)

        # Formatting only for display (not changing the data structure)
        formatted_mean = np.array([f'{val:.4f}' for val in fold_results_mean])
        formatted_std = np.array([f'{val:.4f}' for val in fold_results_std])

        # Printing formatted values to console, still as arrays
        print(f"Fold results: {fold_results}\nMean: {formatted_mean}\nStd: {formatted_std}")

        # Saving formatted values to file, maintaining array format for mean and std
        with open(os.path.join(args.output_dir, "fold_results.txt"), mode="a") as f:
            print(f"Fold results: {fold_results}\nMean: {formatted_mean}\nStd: {formatted_std}", file=f)

        # Calculate average AUC and accuracy and std
        fold_results_test = np.array(fold_results_test)
        fold_results_mean_test = np.mean(fold_results_test, axis=0)
        fold_results_std_test = np.std(fold_results_test, axis=0)

        # Formatting only for display (not changing the data structure)
        formatted_mean = np.array([f'{val:.4f}' for val in fold_results_mean_test])
        formatted_std = np.array([f'{val:.4f}' for val in fold_results_std_test])

        # Printing formatted values to console, still as arrays
        print(f"Fold results: {fold_results_test}\nMean: {formatted_mean}\nStd: {formatted_std}")

        # Saving formatted values to file, maintaining array format for mean and std
        with open(os.path.join(args.output_dir, "fold_results_test.txt"), mode="a") as f:
            print(f"Fold results: {fold_results_test}\nMean: {formatted_mean}\nStd: {formatted_std}", file=f)

        # Calculate average AUC and accuracy and std
        fold_results_test_for_best_val = np.array(fold_results_test_for_best_val)
        fold_results_mean_test_for_best_val = np.mean(fold_results_test_for_best_val, axis=0)
        fold_results_std_test_for_best_val = np.std(fold_results_test_for_best_val, axis=0)

        # Formatting only for display (not changing the data structure)
        formatted_mean = np.array([f'{val:.4f}' for val in fold_results_mean_test_for_best_val])
        formatted_std = np.array([f'{val:.4f}' for val in fold_results_std_test_for_best_val])

        # Printing formatted values to console, still as arrays
        print(f"Fold results: {fold_results_test_for_best_val}\nMean: {formatted_mean}\nStd: {formatted_std}")

        # Saving formatted values to file, maintaining array format for mean and std
        with open(os.path.join(args.output_dir, "fold_results_test_for_best_val.txt"), mode="a") as f:
            print(f"Fold results: {fold_results_test_for_best_val}\nMean: {formatted_mean}\nStd: {formatted_std}", file=f)


if __name__ == '__main__':
    args = get_args_parser()
    args = args.parse_args()

    if args.output_dir:
        Path(args.output_dir).mkdir(parents=True, exist_ok=True)
    main(args)
