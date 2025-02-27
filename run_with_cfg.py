# Copyright (c) 2018-present, Facebook, Inc.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#

import numpy as np

from common.arguments import parse_args, parse_yacs_args
from configs import *
import torch

import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import os
import sys
import errno
import valeodata

valeodata.download('DriveAndAct')

from common.camera import *
from common.model import *
from common.loss import *
from common.generators import ChunkedGenerator, UnchunkedGenerator
from time import time
from common.utils import deterministic_random, fixseed
from torch.utils.tensorboard import SummaryWriter

fixseed(44)

dad_metadata = {
    'layout_name': 'dad',
    'num_joints': 17,
    'keypoints_symmetry': [
        [1, 3, 5, 7, 9, 11, 13, 15],
        [2, 4, 6, 8, 10, 12, 14, 16],
    ]
}

args = parse_yacs_args()
update_config(cfg, args)
print(cfg)

try:
    # Create checkpoint directory if it does not exist
    if cfg.TRAIN.IS_TRAIN and not os.path.isfile(cfg.LOGS.CHECKPOINT):
        os.makedirs(cfg.LOGS.CHECKPOINT)
except OSError as e:
    if e.errno != errno.EEXIST:
        raise RuntimeError('Unable to create checkpoint directory:', cfg.LOGS.CHECKPOINT)

print('Loading dataset...')
dataset_path = '/datasets_local/DriveAndAct/data_3d_' + cfg.DATASET.DATASET + '_train.npz'
if cfg.DATASET.DATASET == 'h36m':
    from common.h36m_dataset import Human36mDataset
    dataset = Human36mDataset(dataset_path)
elif cfg.DATASET.DATASET == 'dad':
    from common.dad_dataset import DadHuman36MDataset
    dataset = DadHuman36MDataset(dataset_path)
elif cfg.DATASET.DATASET.startswith('humaneva'):
    from common.humaneva_dataset import HumanEvaDataset
    dataset = HumanEvaDataset(dataset_path)
elif cfg.DATASET.DATASET.startswith('custom'):
    from common.custom_dataset import CustomDataset
    dataset = CustomDataset('/datasets_local/DriveAndAct/data_2d_' + cfg.DATASET.DATASET + '_' + cfg.DATASET.KEYPOINTS + '.npz')
else:
    raise KeyError('Invalid dataset')

print('Preparing data...')
print("Subjects: ", dataset.subjects())
for subject in dataset.subjects():
    for action in dataset[subject].keys():
        anim = dataset[subject][action]
        
        if 'positions' in anim:
            positions_3d = []
            for cam in anim['cameras']:
                # pos_3d = world_to_camera(anim['positions'], R=cam['orientation'], t=cam['translation'])
                # Replace the previous line by:
                pos_3d = anim['positions'][0]
                pos_3d[:, 1:] -= pos_3d[:, :1] # Remove global offset, but keep trajectory in first position
                positions_3d.append(pos_3d)
            anim['positions_3d'] = positions_3d

print('Loading 2D detections...')
keypoints = np.load('/datasets_local/DriveAndAct/data_2d_' + cfg.DATASET.DATASET + '_' + cfg.DATASET.KEYPOINTS + '.npz', allow_pickle=True)
keypoints_metadata = dad_metadata
keypoints_symmetry = dad_metadata['keypoints_symmetry']
kps_left, kps_right = list(keypoints_symmetry[0]), list(keypoints_symmetry[1])
joints_left, joints_right = list(dataset.skeleton().joints_left()), list(dataset.skeleton().joints_right())
keypoints = keypoints['positions_2d'].item()

for subject in dataset.subjects():
    assert subject in keypoints, 'Subject {} is missing from the 2D detections dataset'.format(subject)
    for action in dataset[subject].keys():
        assert action in keypoints[subject], 'Action {} of subject {} is missing from the 2D detections dataset'.format(action, subject)
        if 'positions_3d' not in dataset[subject][action]:
            continue
            
        # for cam_idx in range(len(keypoints[subject][action])):
            
        # We check for >= instead of == because some videos in H3.6M contain extra frames
        mocap_length = dataset[subject][action]['positions_3d'][0].shape[0]
        assert keypoints[subject][action].shape[0] >= mocap_length
        
        if keypoints[subject][action].shape[0] > mocap_length:
            # Shorten sequence
            keypoints[subject][action] = keypoints[subject][action][:mocap_length]

        assert len(keypoints[subject][action]) == len(dataset[subject][action]['positions_3d'][0])
        
for subject in keypoints.keys():
    for i, action in enumerate(keypoints[subject]):
        kps = keypoints[subject][action].reshape((len(keypoints[subject][action]), 17, 3))
        # Normalize camera frame
        cam = dataset.cameras()[subject][i]
        kps[..., :2] = normalize_screen_coordinates(kps[..., :2], w=cam['res_w'], h=cam['res_h'])
        keypoints[subject][action] = kps[..., :2]
subjects_train = cfg.DATASET.SUBJECTS_TRAIN.split(',')
subjects_semi = [] if not cfg.DATASET.SUBJECTS_UNLABELED else cfg.DATASET.SUBJECTS_UNLABELED.split(',')
if not cfg.VIS.RENDER:
    subjects_test = cfg.DATASET.SUBJECTS_TEST.split(',')
else:
    subjects_test = [cfg.VIS.SUBJECT]

semi_supervised = len(subjects_semi) > 0
if semi_supervised and not dataset.supports_semi_supervised():
    raise RuntimeError('Semi-supervised training is not implemented for this dataset')
            
def fetch(subjects, action_filter=None, subset=1, parse_3d_poses=True):
    out_poses_3d = []
    out_poses_2d = []
    out_camera_params = []
    for subject in subjects:
        for i, action in enumerate(keypoints[subject].keys()):
            if action_filter is not None:
                found = False
                for a in action_filter:
                    if action.startswith(a):
                        found = True
                        break
                if not found:
                    continue
                
            poses_2d = np.array([keypoints[subject][action]])
            
            for i in range(len(poses_2d)): # Iterate across cameras
                out_poses_2d.append(poses_2d[i][cfg.LOGS.SEQ_START: cfg.LOGS.SEQ_START + cfg.LOGS.SEQ_LENGTH])
                
               
            # Replaced by
            # out_poses_2d.append(poses_2d)
            
            if subject in dataset.cameras():
                cams = [dataset.cameras()[subject][i]]
                assert len(cams) == len(poses_2d), 'Camera count mismatch'
                for cam in cams:
                    if 'intrinsic' in cam:
                        out_camera_params.append(cam['intrinsic'])
                
            if parse_3d_poses and 'positions_3d' in dataset[subject][action]:
                poses_3d = dataset[subject][action]['positions_3d']
                assert len(poses_3d) == len(poses_2d), 'Camera count mismatch'
                for i in range(len(poses_3d)): # Iterate across cameras
                    pose_3d = poses_3d[i][cfg.LOGS.SEQ_START: cfg.LOGS.SEQ_START + cfg.LOGS.SEQ_LENGTH]
                    pose_3d = np.reshape(pose_3d, (len(pose_3d), 17, 3))
                    out_poses_3d.append(pose_3d)
    
    if len(out_camera_params) == 0:
        out_camera_params = None
    if len(out_poses_3d) == 0:
        out_poses_3d = None
    
    stride = cfg.EXPS.DOWNSAMPLE
    if subset < 1:
        for i in range(len(out_poses_2d)):
            n_frames = int(round(len(out_poses_2d[i])//stride * subset)*stride)
            start = deterministic_random(0, len(out_poses_2d[i]) - n_frames + 1, str(len(out_poses_2d[i])))
            out_poses_2d[i] = out_poses_2d[i][start:start+n_frames:stride]
            if out_poses_3d is not None:
                out_poses_3d[i] = out_poses_3d[i][start:start+n_frames:stride]
    elif stride > 1:
        # Downsample as requested
        for i in range(len(out_poses_2d)):
            out_poses_2d[i] = out_poses_2d[i][::stride]
            if out_poses_3d is not None:
                out_poses_3d[i] = out_poses_3d[i][::stride]
    
    
    return out_camera_params, out_poses_3d, out_poses_2d

action_filter = None if cfg.DATASET.ACTIONS == '*' else cfg.DATASET.ACTIONS.split(',')
if action_filter is not None:
    print('Selected actions:', action_filter)
    
print("Loading GT Poses ...")
cameras_valid, poses_valid, poses_valid_2d = fetch(subjects_test, action_filter)
print("\n Loaded shapes: cam {}, pose_v {} {}, pose_v_2d {} {}".format(cameras_valid[0].shape, len(poses_valid), poses_valid[0].shape, len(poses_valid_2d), poses_valid_2d[0].shape))


print("Loading Model ...")
filter_widths = [int(x) for x in cfg.MODEL.ARCHITECTURE] # removed.split(',')
print("Filter Width ", filter_widths)
if not cfg.EXPS.DISABLE_OPTIMIZATIONS and not cfg.EXPS.DENSE and cfg.MODEL.STRIDE == 1:
    print("Loading Model Pos Train... 1")
    # Use optimized model for single-frame predictions
    print(poses_valid_2d[0].shape[-2], poses_valid_2d[0].shape[-1])
    model_pos_train = TemporalModelOptimized1f(poses_valid_2d[0].shape[-2], poses_valid_2d[0].shape[-1], dataset.skeleton().num_joints(),
                                filter_widths=filter_widths, causal=cfg.MODEL.CAUSAL, dropout=cfg.MODEL.DROPOUT, channels=cfg.MODEL.CHANNELS)
else:
    print("Loading Model Pos Train... 2")
    # When incompatible settings are detected (stride > 1, dense filters, or disabled optimization) fall back to normal model
    model_pos_train = TemporalModel(poses_valid_2d[0].shape[-2], poses_valid_2d[0].shape[-1], dataset.skeleton().num_joints(),
                                filter_widths=filter_widths, causal=cfg.MODEL.CAUSAL, dropout=cfg.MODEL.DROPOUT, channels=cfg.MODEL.CHANNELS,
                                dense=cfg.EXPS.DENSE)
    
model_pos = TemporalModel(poses_valid_2d[0].shape[-2], poses_valid_2d[0].shape[-1], dataset.skeleton().num_joints(),
                            filter_widths=filter_widths, causal=cfg.MODEL.CAUSAL, dropout=cfg.MODEL.DROPOUT, channels=cfg.MODEL.CHANNELS,
                            dense=cfg.EXPS.DENSE)
print("Model loaded!")
receptive_field = model_pos.receptive_field()
print('INFO: Receptive field: {} frames'.format(receptive_field))
pad = (receptive_field - 1) // 2 # Padding on each side
if cfg.MODEL.CAUSAL:
    print('INFO: Using causal convolutions')
    causal_shift = pad
else:
    causal_shift = 0

model_params = 0
for parameter in model_pos.parameters():
    model_params += parameter.numel()
print('INFO: Trainable parameter count:', model_params)

if torch.cuda.is_available():
    model_pos = model_pos.cuda()
    model_pos_train = model_pos_train.cuda()
    
if cfg.TRAIN.RESUME or cfg.TRAIN.EVALUATE:
    chk_filename = os.path.join(cfg.LOGS.CHECKPOINT, cfg.TRAIN.RESUME if cfg.TRAIN.RESUME else cfg.TRAIN.EVALUATE)
    if cfg.TRAIN.EVALUATE:
        chk_filename = os.path.join(cfg.TRAIN.EVALUATE)
    print('Loading checkpoint', chk_filename)
    checkpoint = torch.load(chk_filename, map_location=lambda storage, loc: storage)
    print('This model was trained for {} epochs'.format(checkpoint['epoch']))
    model_pos_train.load_state_dict(checkpoint['model_pos'])
    model_pos.load_state_dict(checkpoint['model_pos'])
    
    print(checkpoint['model_traj'])
    # if cfg.TRAIN.EVALUATE and 'model_traj' in checkpoint:
    if cfg.TRAIN.EVALUATE and checkpoint["model_traj"] is not None:
        # Load trajectory model if it contained in the checkpoint (e.g. for inference in the wild)
        model_traj = TemporalModel(poses_valid_2d[0].shape[-2], poses_valid_2d[0].shape[-1], 1,
                            filter_widths=filter_widths, causal=cfg.MODEL.CAUSAL, dropout=cfg.MODEL.DROPOUT, channels=cfg.MODEL.CHANNELS,
                            dense=cfg.EXPS.DENSE)
        if torch.cuda.is_available():
            model_traj = model_traj.cuda()
        model_traj.load_state_dict(checkpoint['model_traj'])
    else:
        model_traj = None
        
    
test_generator = UnchunkedGenerator(cameras_valid, poses_valid, poses_valid_2d,
                                    pad=pad, causal_shift=causal_shift, augment=False,
                                    kps_left=kps_left, kps_right=kps_right, joints_left=joints_left, joints_right=joints_right)
print('INFO: Testing on {} frames'.format(test_generator.num_frames()))

if not cfg.TRAIN.EVALUATE:
    cameras_train, poses_train, poses_train_2d = fetch(subjects_train, action_filter, subset=cfg.EXPS.SUBSET)

    lr = cfg.MODEL.LR
    if semi_supervised:
        cameras_semi, _, poses_semi_2d = fetch(subjects_semi, action_filter, parse_3d_poses=False)
        
        if not cfg.EXPS.DISABLE_OPTIMIZATIONS and not cfg.EXPS.DENSE and cfg.MODEL.STRIDE == 1:
            # Use optimized model for single-frame predictions
            model_traj_train = TemporalModelOptimized1f(poses_valid_2d[0].shape[-2], poses_valid_2d[0].shape[-1], 1,
                    filter_widths=filter_widths, causal=cfg.MODEL.CAUSAL, dropout=cfg.MODEL.DROPOUT, channels=cfg.MODEL.CHANNELS)
        else:
            # When incompatible settings are detected (stride > 1, dense filters, or disabled optimization) fall back to normal model
            model_traj_train = TemporalModel(poses_valid_2d[0].shape[-2], poses_valid_2d[0].shape[-1], 1,
                    filter_widths=filter_widths, causal=cfg.MODEL.CAUSAL, dropout=cfg.MODEL.DROPOUT, channels=cfg.MODEL.CHANNELS,
                    dense=cfg.EXPS.DENSE)
        
        model_traj = TemporalModel(poses_valid_2d[0].shape[-2], poses_valid_2d[0].shape[-1], 1,
                            filter_widths=filter_widths, causal=cfg.MODEL.CAUSAL, dropout=cfg.MODEL.DROPOUT, channels=cfg.MODEL.CHANNELS,
                            dense=cfg.EXPS.DENSE)
        if torch.cuda.is_available():
            model_traj = model_traj.cuda()
            model_traj_train = model_traj_train.cuda()
        optimizer = optim.Adam(list(model_pos_train.parameters()) + list(model_traj_train.parameters()),
                               lr=lr, amsgrad=True)
        
        losses_2d_train_unlabeled = []
        losses_angle_train_unlabeled = []
        losses_head_angle_train_unlabeled = []
        losses_left_arm_angle_train_unlabeled = []
        losses_right_arm_angle_train_unlabeled = []
        losses_left_leg_angle_train_unlabeled = []
        losses_right_leg_angle_train_unlabeled = []
        losses_symmetry_unlabeled = []
        losses_bone_length = []
        losses_2d_train_labeled_eval = []
        losses_2d_train_unlabeled_eval = []
        losses_2d_valid = []

        losses_traj_train = []
        losses_traj_train_eval = []
        losses_traj_valid = []
    else:
        optimizer = optim.Adam(model_pos_train.parameters(), lr=lr, amsgrad=True)
        
    lr_decay = cfg.MODEL.LR_DECAY

    losses_3d_train = []
    losses_3d_train_eval = []
    losses_3d_valid = []
    losses_angle_train = []
    losses_head_angle_train = []
    losses_left_arm_angle_train = []
    losses_right_arm_angle_train = []
    losses_left_leg_angle_train = []
    losses_right_leg_angle_train = []
    losses_symmetry = []

    epoch = 0
    initial_momentum = 0.1
    final_momentum = 0.001
    
    
    train_generator = ChunkedGenerator(cfg.MODEL.BATCH_SIZE//cfg.MODEL.STRIDE, cameras_train, poses_train, poses_train_2d, cfg.MODEL.STRIDE,
                                       pad=pad, causal_shift=causal_shift, shuffle=True, augment=cfg.MODEL.AUGMENTATION,
                                       kps_left=kps_left, kps_right=kps_right, joints_left=joints_left, joints_right=joints_right)
    train_generator_eval = UnchunkedGenerator(cameras_train, poses_train, poses_train_2d,
                                              pad=pad, causal_shift=causal_shift, augment=False)
    print('INFO: Training on {} frames'.format(train_generator_eval.num_frames()))
    if semi_supervised:
        semi_generator = ChunkedGenerator(cfg.MODEL.BATCH_SIZE//cfg.MODEL.STRIDE, cameras_semi, None, poses_semi_2d, cfg.MODEL.STRIDE,
                                          pad=pad, causal_shift=causal_shift, shuffle=True,
                                          random_seed=4321, augment=cfg.MODEL.AUGMENTATION,
                                          kps_left=kps_left, kps_right=kps_right, joints_left=joints_left, joints_right=joints_right,
                                          endless=True)
        semi_generator_eval = UnchunkedGenerator(cameras_semi, None, poses_semi_2d,
                                                 pad=pad, causal_shift=causal_shift, augment=False)
        print('INFO: Semi-supervision on {} frames'.format(semi_generator_eval.num_frames()))

    if cfg.TRAIN.RESUME:
        epoch = checkpoint['epoch']
        if 'optimizer' in checkpoint and checkpoint['optimizer'] is not None:
            optimizer.load_state_dict(checkpoint['optimizer'])
            train_generator.set_random_state(checkpoint['random_state'])
        else:
            print('WARNING: this checkpoint does not contain an optimizer state. The optimizer will be reinitialized.')
        
        lr = checkpoint['lr']
        if semi_supervised:
            model_traj_train.load_state_dict(checkpoint['model_traj'])
            model_traj.load_state_dict(checkpoint['model_traj'])
            semi_generator.set_random_state(checkpoint['random_state_semi'])
            
    print('** Note: reported losses are averaged over all frames and test-time augmentation is not used here.')
    print('** The final evaluation will be carried out after the last training epoch.')
    
    writer = SummaryWriter(log_dir=cfg.LOGS.TENSORBOARD)

    
    # Pos model only
    while epoch < cfg.MODEL.EPOCHS:
        start_time = time()
        epoch_loss_3d_train = 0
        epoch_loss_traj_train = 0
        epoch_loss_2d_train_unlabeled = 0
        epoch_loss_angle = 0
        epoch_loss_left_arm_angle = 0
        epoch_loss_right_arm_angle = 0
        epoch_loss_left_leg_angle = 0
        epoch_loss_right_leg_angle = 0
        epoch_loss_head_angle = 0
        epoch_loss_symmetry = 0
        epoch_loss_angle_unlabeled = 0
        epoch_loss_left_arm_angle_unlabeled = 0
        epoch_loss_right_arm_angle_unlabeled = 0
        epoch_loss_left_leg_angle_unlabeled = 0
        epoch_loss_right_leg_angle_unlabeled = 0
        epoch_loss_head_angle_unlabeled = 0
        epoch_loss_symmetry_unlabeled = 0
        
        N = 0
        N_semi = 0
        model_pos_train.train()
        batch_id = 0
        if semi_supervised:
            # Semi-supervised scenario
            model_traj_train.train()
            for (_, batch_3d, batch_2d), (cam_semi, _, batch_2d_semi) in \
                zip(train_generator.next_epoch(), semi_generator.next_epoch()):
                
                # Fall back to supervised training for the first epoch (to avoid instability)
                skip = epoch < cfg.EXPS.WARMUP
                
                cam_semi = torch.from_numpy(cam_semi.astype('float32'))
                inputs_3d = torch.from_numpy(batch_3d.astype('float32'))
                if torch.cuda.is_available():
                    cam_semi = cam_semi.cuda()
                    inputs_3d = inputs_3d.cuda()
                    
                inputs_traj = inputs_3d[:, :, :1].clone()
                inputs_3d[:, :, 0] = 0
                
                # Split point between labeled and unlabeled samples in the batch
                split_idx = inputs_3d.shape[0]
                

                inputs_2d = torch.from_numpy(batch_2d.astype('float32'))
                inputs_2d_semi = torch.from_numpy(batch_2d_semi.astype('float32'))
                if torch.cuda.is_available():
                    inputs_2d = inputs_2d.cuda()
                    inputs_2d_semi = inputs_2d_semi.cuda()
                inputs_2d_cat =  torch.cat((inputs_2d, inputs_2d_semi), dim=0) if not skip else inputs_2d

                optimizer.zero_grad()

                # Compute 3D poses
                predicted_3d_pos_cat = model_pos_train(inputs_2d_cat)

                loss_3d_pos = mpjpe(predicted_3d_pos_cat[:split_idx], inputs_3d)
                epoch_loss_3d_train += inputs_3d.shape[0]*inputs_3d.shape[1] * loss_3d_pos.item()
                N += inputs_3d.shape[0]*inputs_3d.shape[1]
                if not skip and cfg.EXPS.CONSTRAIN_3D:
                    loss_3d_pos *= cfg.EXPS.LAMBDA_3D
                loss_total = loss_3d_pos
                
                # Add logs to tensorboard
                writer.add_scalar('Train/Loss_pose_3d', loss_3d_pos, epoch * train_generator.num_batches + batch_id)

                # Compute global trajectory
                predicted_traj_cat = model_traj_train(inputs_2d_cat)
                w = 1 / inputs_traj[:, :, :, 2] # Weight inversely proportional to depth
                w[w == float("Inf")] = 0 # Let the occluded keypoints values to zero
                if True in torch.isinf(w):
                    print("Encountered inf value in w")
                loss_traj = weighted_mpjpe(predicted_traj_cat[:split_idx], inputs_traj, w)
                epoch_loss_traj_train += inputs_3d.shape[0]*inputs_3d.shape[1] * loss_traj.item()
                assert inputs_traj.shape[0]*inputs_traj.shape[1] == inputs_3d.shape[0]*inputs_3d.shape[1]
                loss_total += loss_traj
                
                # Add logs to tensorboard
                writer.add_scalar('Train/Loss_trajectory', loss_traj, epoch * train_generator.num_batches + batch_id)
                

                # Bone length term to enforce kinematic constraints
                if cfg.EXPS.BONE_SYM and skip:
                    predicted_3d_pos = predicted_3d_pos_cat[:split_idx]
                    dists = predicted_3d_pos[:, :, 1:] - predicted_3d_pos[:, :, dataset.skeleton().parents()[1:]]
                    bone_lengths = torch.mean(torch.norm(dists, dim=3), dim=1)
                    penalty = cfg.EXPS.LAMBDA_SYM * torch.mean(torch.abs(torch.mean(bone_lengths[[0,2,4,6,8,10,12,14]], dim=0) - torch.mean(bone_lengths[[1,3,5,7,9,11,13,15]], dim=0)))# replace by left kpts
                    loss_total += penalty
                    epoch_loss_symmetry += inputs_3d.shape[0]*inputs_3d.shape[1] * penalty.item() / cfg.EXPS.LAMBDA_SYM
                    # Add logs to tensorboard
                    writer.add_scalar('Train_Penalty/Bones_symmetry_penalty', penalty, epoch * train_generator.num_batches + batch_id)

                if cfg.EXPS.ILLEGAL_ANGLE and skip:
                    # Compute the vectors associated with bones
                    up_left_loss, up_right_loss, low_left_loss, low_right_loss, head_loss = angle_error(predicted_3d_pos, debug=cfg.DEBUG)

                    # Compute the angle between these vectors
                    angles_penalty = cfg.EXPS.LAMBDA_ANGLE * (torch.mean(head_loss * torch.exp(head_loss)) \
                        + torch.mean(low_right_loss * torch.exp(low_right_loss)) \
                        + torch.mean(low_left_loss * torch.exp(low_left_loss)) \
                        + torch.mean(up_left_loss * torch.exp(up_left_loss)) \
                        + torch.mean(up_right_loss * torch.exp(up_right_loss)))
                                    
                    loss_total += angles_penalty
                    epoch_loss_angle += inputs_3d.shape[0]*inputs_3d.shape[1] * angles_penalty.item() / cfg.EXPS.LAMBDA_ANGLE
                    epoch_loss_head_angle += inputs_3d.shape[0]*inputs_3d.shape[1] * torch.mean(head_loss * torch.exp(head_loss)).item()
                    epoch_loss_left_arm_angle += inputs_3d.shape[0]*inputs_3d.shape[1] * torch.mean(up_left_loss * torch.exp(up_left_loss)).item()
                    epoch_loss_right_arm_angle += inputs_3d.shape[0]*inputs_3d.shape[1] * torch.mean(up_right_loss * torch.exp(up_right_loss)).item()
                    epoch_loss_left_leg_angle += inputs_3d.shape[0]*inputs_3d.shape[1] * torch.mean(low_left_loss * torch.exp(low_left_loss)).item()
                    epoch_loss_right_leg_angle += inputs_3d.shape[0]*inputs_3d.shape[1] * torch.mean(low_right_loss * torch.exp(low_right_loss)).item()
                    
                    # Add logs to tensorboard
                    writer.add_scalar('Train_labeled_Penalty/Illegal_angle_penalty', angles_penalty, epoch * train_generator.num_batches + batch_id)
                    writer.add_scalar('Train_labeled_Penalty/Head_angle_penalty', torch.mean(head_loss * torch.exp(head_loss)), epoch * train_generator.num_batches + batch_id)
                    writer.add_scalar('Train_labeled_Penalty/Up_left_angle_penalty', torch.mean(up_left_loss * torch.exp(up_left_loss)), epoch * train_generator.num_batches + batch_id)
                    writer.add_scalar('Train_labeled_Penalty/Up_right_angle_penalty', torch.mean(up_right_loss * torch.exp(up_right_loss)), epoch * train_generator.num_batches + batch_id)
                    writer.add_scalar('Train_labeled_Penalty/Low_left_angle_penalty', torch.mean(low_left_loss * torch.exp(low_left_loss)), epoch * train_generator.num_batches + batch_id)
                    writer.add_scalar('Train_labeled_Penalty/Low_right_angle_penalty', torch.mean(low_right_loss * torch.exp(low_right_loss)), epoch * train_generator.num_batches + batch_id)
                    ############################################## END ADDED ######################################################################
                    

                if not skip:
                    # Semi-supervised loss for unlabeled samples
                    predicted_semi = predicted_3d_pos_cat[split_idx:]
                    if pad > 0:
                        target_semi = inputs_2d_semi[:, pad:-pad, :, :2].contiguous()
                    else:
                        target_semi = inputs_2d_semi[:, :, :, :2].contiguous()
                        
                    projection_func = project_to_2d_linear if cfg.EXPS.LINEAR_PROJECTION else project_to_2d
                    reconstruction_semi = projection_func(predicted_semi + predicted_traj_cat[split_idx:], cam_semi)

                    loss_reconstruction = mpjpe(reconstruction_semi, target_semi) # On 2D poses
                    
                    epoch_loss_2d_train_unlabeled += predicted_semi.shape[0]*predicted_semi.shape[1] * loss_reconstruction.item()
                    if not cfg.EXPS.NO_PROJ:
                        loss_total += loss_reconstruction
                       
                    
                    # Bone length term to enforce kinematic constraints
                    if cfg.EXPS.BONE_LENGTH:
                        visible = inputs_3d.clone()
                        visible[visible != 0] = 1
                        visible_bones = visible[:, :, 1:]*visible[:,  :, dataset.skeleton().parents()[1:]]
                        dists = predicted_3d_pos_cat[:, :, 1:] - predicted_3d_pos_cat[:, :, dataset.skeleton().parents()[1:]]
                        bone_lengths = torch.mean(torch.norm(dists, dim=3), dim=1)
                        penalty = torch.mean(torch.abs(torch.mean(bone_lengths[:split_idx], dim=0) \
                                                     - torch.mean(bone_lengths[split_idx:], dim=0)))
                        loss_total += penalty
                        # Add logs to tensorboard
                        writer.add_scalar('Train_Semi_Penalty/Loss_bones_length_penalty', penalty, epoch * train_generator.num_batches + batch_id)

                    ########################################################## ADDED ################################################
                    # Bone length term to enforce kinematic constraints
                    if cfg.EXPS.BONE_SYM:
  
                        semi_dists = predicted_semi[:, :, 1:] - predicted_semi[:, :, dataset.skeleton().parents()[1:]]
                        semi_bone_lengths = torch.mean(torch.norm(semi_dists, dim=3), dim=1)[0]
                        semi_penalty = torch.mean(torch.abs(torch.mean(semi_bone_lengths[[0,2,4,6,8,10,12,14]], dim=0) - torch.mean(semi_bone_lengths[[1,3,5,7,9,11,13,15]], dim=0)))# replace by left kpts
                        loss_total += cfg.EXPS.LAMBDA_SYM * semi_penalty
                        epoch_loss_symmetry_unlabeled += predicted_semi.shape[0]*predicted_semi.shape[1] * semi_penalty.item()
                        # Add logs to tensorboard
                        writer.add_scalar('Train_Semi_Penalty/Bones_symmetry_penalty', semi_penalty, epoch * train_generator.num_batches + batch_id)

                    if cfg.EXPS.ILLEGAL_ANGLE:
                        # Compute the vectors associated with bones
                        up_left_loss, up_right_loss, low_left_loss, low_right_loss, head_loss = angle_error(predicted_semi, debug=cfg.DEBUG)

                        # Compute the angle between these vectors
                        semi_angles_penalty = cfg.EXPS.LAMBDA_ANGLE * (torch.mean(head_loss * torch.exp(head_loss)) \
                                                            + torch.mean(low_right_loss * torch.exp(low_right_loss)) \
                                                            + torch.mean(low_left_loss * torch.exp(low_left_loss)) \
                                                            + torch.mean(up_left_loss * torch.exp(up_left_loss)) \
                                                            + torch.mean(up_right_loss * torch.exp(up_right_loss)))
                        loss_total += semi_angles_penalty
                        # Add logs to tensorboard
                        writer.add_scalar('Train_unlabeled_Penalty/Illegal_angle_penalty', semi_angles_penalty, epoch * train_generator.num_batches + batch_id)
                        writer.add_scalar('Train_unlabeled_Penalty/Head_angle_penalty', torch.mean(head_loss * torch.exp(head_loss)), epoch * train_generator.num_batches + batch_id)
                        writer.add_scalar('Train_unlabeled_Penalty/Up_left_angle_penalty', torch.mean(up_left_loss * torch.exp(up_left_loss)), epoch * train_generator.num_batches + batch_id)
                        writer.add_scalar('Train_unlabeled_Penalty/Up_right_angle_penalty', torch.mean(up_right_loss * torch.exp(up_right_loss)), epoch * train_generator.num_batches + batch_id)
                        writer.add_scalar('Train_unlabeled_Penalty/Low_left_angle_penalty', torch.mean(low_left_loss * torch.exp(low_left_loss)), epoch * train_generator.num_batches + batch_id)
                        writer.add_scalar('Train_unlabeled_Penalty/Low_right_angle_penalty', torch.mean(low_right_loss * torch.exp(low_right_loss)), epoch * train_generator.num_batches + batch_id)
                        
                        epoch_loss_angle_unlabeled += predicted_semi.shape[0]*predicted_semi.shape[1] * semi_angles_penalty.item() / cfg.EXPS.LAMBDA_ANGLE
                        epoch_loss_head_angle_unlabeled += predicted_semi.shape[0]*predicted_semi.shape[1] * torch.mean(head_loss * torch.exp(head_loss)).item()
                        epoch_loss_left_arm_angle_unlabeled += predicted_semi.shape[0]*predicted_semi.shape[1] * torch.mean(up_left_loss * torch.exp(up_left_loss)).item()
                        epoch_loss_right_arm_angle_unlabeled += predicted_semi.shape[0]*predicted_semi.shape[1] * torch.mean(up_right_loss * torch.exp(up_right_loss)).item()
                        epoch_loss_left_leg_angle_unlabeled += predicted_semi.shape[0]*predicted_semi.shape[1] * torch.mean(low_left_loss * torch.exp(low_left_loss)).item()
                        epoch_loss_right_leg_angle_unlabeled += predicted_semi.shape[0]*predicted_semi.shape[1] * torch.mean(low_right_loss * torch.exp(low_right_loss)).item()
                        
                        # Compute the vectors associated with bones
                        up_left_loss, up_right_loss, low_left_loss, low_right_loss, head_loss = angle_error(predicted_3d_pos, debug=cfg.DEBUG)

                        # Compute the angle between these vectors
                        angles_penalty =  cfg.EXPS.LAMBDA_ANGLE * (torch.mean(head_loss * torch.exp(head_loss)) \
                                                            + torch.mean(low_right_loss * torch.exp(low_right_loss)) \
                                                            + torch.mean(low_left_loss * torch.exp(low_left_loss)) \
                                                            + torch.mean(up_left_loss * torch.exp(up_left_loss)) \
                                                            + torch.mean(up_right_loss * torch.exp(up_right_loss)))
                        if cfg.EXPS.DECAY_ANGLE_LAB != 1.:
                            angles_penalty *= cfg.EXPS.DECAY_ANGLE_LAB
                        # loss_total += angles_penalty
                        ############################################## END ADDED ######################################################################
                            
                    
                    N_semi += predicted_semi.shape[0]*predicted_semi.shape[1]
                     # Add logs to tensorboard
                    writer.add_scalar('Train_unlabeled_Penalty/Loss_reconstruction', epoch_loss_2d_train_unlabeled / N_semi, epoch * train_generator.num_frames() + batch_id)

                else:
                    N_semi += 1 # To avoid division by zero

                loss_total.backward()
                batch_id += train_generator.batch_size

                optimizer.step()
            losses_traj_train.append(epoch_loss_traj_train / N)
            losses_2d_train_unlabeled.append(epoch_loss_2d_train_unlabeled / N_semi)
            losses_angle_train_unlabeled.append(epoch_loss_angle_unlabeled / N_semi)
            losses_head_angle_train_unlabeled.append(epoch_loss_head_angle_unlabeled  / N_semi)
            losses_left_arm_angle_train_unlabeled.append(epoch_loss_left_arm_angle_unlabeled / N_semi)
            losses_right_arm_angle_train_unlabeled.append(epoch_loss_right_arm_angle_unlabeled / N_semi)
            losses_left_leg_angle_train_unlabeled.append(epoch_loss_left_leg_angle_unlabeled / N_semi)
            losses_right_leg_angle_train_unlabeled.append(epoch_loss_right_leg_angle_unlabeled / N_semi)
            losses_symmetry_unlabeled.append(epoch_loss_symmetry_unlabeled / N_semi)
            
            losses_angle_train.append(epoch_loss_angle / N)
            losses_head_angle_train.append(epoch_loss_head_angle / N)
            losses_left_arm_angle_train.append(epoch_loss_left_arm_angle / N)
            losses_right_arm_angle_train.append(epoch_loss_right_arm_angle / N)
            losses_left_leg_angle_train.append(epoch_loss_left_leg_angle / N)
            losses_right_leg_angle_train.append(epoch_loss_right_leg_angle/ N)
            losses_symmetry.append(epoch_loss_symmetry / N)
            
            # Add logs to tensorboard
            writer.add_scalar('Train/Epoch_loss_traj_train', epoch_loss_traj_train / N, epoch)
            writer.add_scalar('Train/Epoch_loss_2d_unlabeled', epoch_loss_2d_train_unlabeled / N_semi, epoch)

        else:
            # Regular supervised scenario
            batch_id = 0
            for _, batch_3d, batch_2d in train_generator.next_epoch():
                inputs_3d = torch.from_numpy(batch_3d.astype('float32'))
                inputs_2d = torch.from_numpy(batch_2d.astype('float32'))
                
                if torch.cuda.is_available():
                    inputs_3d = inputs_3d.cuda()
                    inputs_2d = inputs_2d.cuda()
                inputs_3d[:, :, 0] = 0

                optimizer.zero_grad()

                # Predict 3D poses
                predicted_3d_pos = model_pos_train(inputs_2d)
                loss_3d_pos = mpjpe(predicted_3d_pos, inputs_3d)
                epoch_loss_3d_train += inputs_3d.shape[0]*inputs_3d.shape[1] * loss_3d_pos.item()
                N += inputs_3d.shape[0]*inputs_3d.shape[1]

                loss_total = loss_3d_pos
                
                
                ########################################################## ADDED ################################################
                # Bone length term to enforce kinematic constraints
                if cfg.EXPS.BONE_SYM:
                    dists = predicted_3d_pos[:, :, 1:] - predicted_3d_pos[:, :, dataset.skeleton().parents()[1:]]
                    bone_lengths = torch.mean(torch.norm(dists, dim=3), dim=1)
                    penalty = cfg.EXPS.LAMBDA_SYM * torch.mean(torch.abs(torch.mean(bone_lengths[[0,2,4,6,8,10,12,14]], dim=0) - torch.mean(bone_lengths[[1,3,5,7,9,11,13,15]], dim=0)))# replace by left kpts
                    loss_total += penalty
                    # Add logs to tensorboard
                    writer.add_scalar('Train_Penalty/Bones_symmetry_penalty', penalty, epoch * train_generator.num_batches + batch_id)

                if cfg.EXPS.ILLEGAL_ANGLE:
                    # Compute the vectors associated with bones
                    up_left_loss, up_right_loss, low_left_loss, low_right_loss, head_loss = angle_error(predicted_3d_pos, debug=cfg.DEBUG)

                    # Compute the angle between these vectors
                    angles_penalty = cfg.EXPS.LAMBDA_ANGLE * (torch.mean(head_loss * torch.exp(head_loss)) \
                        + torch.mean(low_right_loss * torch.exp(low_right_loss)) \
                        + torch.mean(low_left_loss * torch.exp(low_left_loss)) \
                        + torch.mean(up_left_loss * torch.exp(up_left_loss)) \
                        + torch.mean(up_right_loss * torch.exp(up_right_loss)))
                                    
                    loss_total += angles_penalty
                    # Add logs to tensorboard
                    writer.add_scalar('Train_Penalty/Illegal_angle_penalty', angles_penalty, epoch * train_generator.num_batches + batch_id)
                    writer.add_scalar('Train_Penalty/Head_angle_penalty', torch.mean(head_loss * torch.exp(head_loss)), epoch * train_generator.num_batches + batch_id)
                    writer.add_scalar('Train_Penalty/Up_left_angle_penalty', torch.mean(up_left_loss * torch.exp(up_left_loss)), epoch * train_generator.num_batches + batch_id)
                    writer.add_scalar('Train_Penalty/Up_right_angle_penalty', torch.mean(up_right_loss * torch.exp(up_right_loss)), epoch * train_generator.num_batches + batch_id)
                    writer.add_scalar('Train_Penalty/Low_left_angle_penalty', torch.mean(low_left_loss * torch.exp(low_left_loss)), epoch * train_generator.num_batches + batch_id)
                    writer.add_scalar('Train_Penalty/Low_right_angle_penalty', torch.mean(low_right_loss * torch.exp(low_right_loss)), epoch * train_generator.num_batches + batch_id)
                    ############################################## END ADDED ######################################################################
                
                
                loss_total.backward()
                optimizer.step()
                
                # Add logs to tensorboard
                writer.add_scalar('Train/Loss_3d_pose', epoch_loss_3d_train / N, epoch * train_generator.num_batches + batch_id)
                batch_id += train_generator.batch_size

        losses_3d_train.append(epoch_loss_3d_train / N)
        
         # Add logs to tensorboard
        writer.add_scalar('Train/Epoch_loss_3d', epoch_loss_3d_train / N, epoch)

        # End-of-epoch evaluation
        with torch.no_grad():
            model_pos.load_state_dict(model_pos_train.state_dict())
            model_pos.eval()
            if semi_supervised:
                model_traj.load_state_dict(model_traj_train.state_dict())
                model_traj.eval()

            epoch_loss_3d_valid = 0
            epoch_loss_traj_valid = 0
            epoch_loss_2d_valid = 0
            N = 0
            val_batch_id = 0
            
            if cfg.EXPS.EVAL:
                # Evaluate on test set
                for cam, batch, batch_2d in test_generator.next_epoch():
                    inputs_3d = torch.from_numpy(batch.astype('float32'))
                    inputs_2d = torch.from_numpy(batch_2d.astype('float32'))
                    if torch.cuda.is_available():
                        inputs_3d = inputs_3d.cuda()
                        inputs_2d = inputs_2d.cuda()
                    inputs_traj = inputs_3d[:, :, :1].clone()
                    inputs_3d[:, :, 0] = 0

                    # Predict 3D poses
                    predicted_3d_pos = model_pos(inputs_2d)
                    loss_3d_pos = mpjpe(predicted_3d_pos, inputs_3d)
                    epoch_loss_3d_valid += inputs_3d.shape[0]*inputs_3d.shape[1] * loss_3d_pos.item()
                    N += inputs_3d.shape[0]*inputs_3d.shape[1]
                    
                    # Add logs to tensorboard
                    #writer.add_scalar('Validation/Loss_3d_pose', epoch_loss_3d_valid / N, epoch * test_generator.num_frames() + val_batch_id)


                    if semi_supervised:
                        cam = torch.from_numpy(cam.astype('float32'))
                        if torch.cuda.is_available():
                            cam = cam.cuda()

                        predicted_traj = model_traj(inputs_2d)
                        loss_traj = mpjpe(predicted_traj, inputs_traj)
                        epoch_loss_traj_valid += inputs_traj.shape[0]*inputs_traj.shape[1] * loss_traj.item()
                        assert inputs_traj.shape[0]*inputs_traj.shape[1] == inputs_3d.shape[0]*inputs_3d.shape[1]
                        
                        # Add logs to tensorboard
                        writer.add_scalar('Validation/Loss_trajectory', epoch_loss_traj_valid / N, epoch * test_generator.num_frames() + val_batch_id)

                        if pad > 0:
                            target = inputs_2d[:, pad:-pad, :, :2].contiguous()
                        else:
                            target = inputs_2d[:, :, :, :2].contiguous()
                        reconstruction = project_to_2d(predicted_3d_pos + predicted_traj, cam)
                        loss_reconstruction = mpjpe(reconstruction, target) # On 2D poses
                        epoch_loss_2d_valid += reconstruction.shape[0]*reconstruction.shape[1] * loss_reconstruction.item()
                        assert reconstruction.shape[0]*reconstruction.shape[1] == inputs_3d.shape[0]*inputs_3d.shape[1]
                        # Add logs to tensorboard
                        writer.add_scalar('Validation/Loss_reconstruction', loss_reconstruction, epoch*test_generator.num_frames() + val_batch_id)
                    val_batch_id += 1
                losses_3d_valid.append(epoch_loss_3d_valid / N)
                # Add logs to tensorboard
                writer.add_scalar('Validation/Epoch_Loss_3d', epoch_loss_3d_valid / N, epoch)

                if semi_supervised:
                    losses_traj_valid.append(epoch_loss_traj_valid / N)
                    losses_2d_valid.append(epoch_loss_2d_valid / N)
                    
                    writer.add_scalar('Validation/Epoch_Loss_2d', epoch_loss_2d_valid / N, epoch)
                    writer.add_scalar('Validation/Epoch_Loss_2d_traj', epoch_loss_traj_valid / N, epoch)


                # Evaluate on training set, this time in evaluation mode
                epoch_loss_3d_train_eval = 0
                epoch_loss_traj_train_eval = 0
                epoch_loss_2d_train_labeled_eval = 0
                N = 0
                batch_id = 0
                for cam, batch, batch_2d in train_generator_eval.next_epoch():
                    if batch_2d.shape[1] == 0:
                        # This can only happen when downsampling the dataset
                        continue
                        
                    inputs_3d = torch.from_numpy(batch.astype('float32'))
                    inputs_2d = torch.from_numpy(batch_2d.astype('float32'))
                    if torch.cuda.is_available():
                        inputs_3d = inputs_3d.cuda()
                        inputs_2d = inputs_2d.cuda()
                    inputs_traj = inputs_3d[:, :, :1].clone()
                    inputs_3d[:, :, 0] = 0

                    # Compute 3D poses
                    predicted_3d_pos = model_pos(inputs_2d)
                    loss_3d_pos = mpjpe(predicted_3d_pos, inputs_3d)
                    epoch_loss_3d_train_eval += inputs_3d.shape[0]*inputs_3d.shape[1] * loss_3d_pos.item()
                    N += inputs_3d.shape[0]*inputs_3d.shape[1]
                    
                    # Add logs to tensorboard
                    writer.add_scalar('Train_inference/Semi/Loss_3d_pose', epoch_loss_3d_train_eval / N,  epoch)


                    if semi_supervised:
                        cam = torch.from_numpy(cam.astype('float32'))
                        if torch.cuda.is_available():
                            cam = cam.cuda()
                        predicted_traj = model_traj(inputs_2d)
                        loss_traj = mpjpe(predicted_traj, inputs_traj)
                        epoch_loss_traj_train_eval += inputs_traj.shape[0]*inputs_traj.shape[1] * loss_traj.item()
                        assert inputs_traj.shape[0]*inputs_traj.shape[1] == inputs_3d.shape[0]*inputs_3d.shape[1]

                        if pad > 0:
                            target = inputs_2d[:, pad:-pad, :, :2].contiguous()
                        else:
                            target = inputs_2d[:, :, :, :2].contiguous()
                        reconstruction = project_to_2d(predicted_3d_pos + predicted_traj, cam)
                        loss_reconstruction = mpjpe(reconstruction, target)
                        epoch_loss_2d_train_labeled_eval += reconstruction.shape[0]*reconstruction.shape[1] * loss_reconstruction.item()
                        assert reconstruction.shape[0]*reconstruction.shape[1] == inputs_3d.shape[0]*inputs_3d.shape[1]

                    batch_id += 1
                losses_3d_train_eval.append(epoch_loss_3d_train_eval / N)
                writer.add_scalar('Inference/labeled/Epoch_Loss_3d', epoch_loss_3d_train_eval / N, epoch)
                if semi_supervised:
                    losses_traj_train_eval.append(epoch_loss_traj_train_eval / N)
                    losses_2d_train_labeled_eval.append(epoch_loss_2d_train_labeled_eval / N)
                    
                    writer.add_scalar('Inference/labeled/Epoch_Loss_2d', epoch_loss_2d_train_labeled_eval / N, epoch)
                    writer.add_scalar('Inference/labeled/Epoch_Loss_2d_traj', epoch_loss_traj_train_eval / N, epoch)



                # Evaluate 2D loss on unlabeled training set (in evaluation mode)
                epoch_loss_2d_train_unlabeled_eval = 0
                N_semi = 0
                batch_id = 0
                if semi_supervised:
                    for cam, _, batch_2d in semi_generator_eval.next_epoch():
                        cam = torch.from_numpy(cam.astype('float32'))
                        inputs_2d_semi = torch.from_numpy(batch_2d.astype('float32'))
                        if torch.cuda.is_available():
                            cam = cam.cuda()
                            inputs_2d_semi = inputs_2d_semi.cuda()

                        predicted_3d_pos_semi = model_pos(inputs_2d_semi)
                        predicted_traj_semi = model_traj(inputs_2d_semi)
                        if pad > 0:
                            target_semi = inputs_2d_semi[:, pad:-pad, :, :2].contiguous()
                        else:
                            target_semi = inputs_2d_semi[:, :, :, :2].contiguous()
                        reconstruction_semi = project_to_2d(predicted_3d_pos_semi + predicted_traj_semi, cam)
                        loss_reconstruction_semi = mpjpe(reconstruction_semi, target_semi)

                        epoch_loss_2d_train_unlabeled_eval += reconstruction_semi.shape[0]*reconstruction_semi.shape[1] \
                                                              * loss_reconstruction_semi.item()
                        N_semi += reconstruction_semi.shape[0]*reconstruction_semi.shape[1]
                        batch_id += 1
                    losses_2d_train_unlabeled_eval.append(epoch_loss_2d_train_unlabeled_eval / N_semi)
                    writer.add_scalar('Inference/unlabeled/Epoch_Loss_2d', epoch_loss_2d_train_unlabeled_eval / N_semi, epoch)
                        

        elapsed = (time() - start_time)/60
        
        if not cfg.EXPS.EVAL:
            print('[%d] time %.2f lr %f 3d_train %f' % (
                    epoch + 1,
                    elapsed,
                    lr,
                    losses_3d_train[-1] * 1000))
        else:
            if semi_supervised:
                print('[%d] time %.2f lr %f '
                      '\n     3d_train %.3f 3d_eval %.3f 3d_valid %.3f '
                      '\n     traj_eval  %.3f traj_valid  %.3f'
                      '\n     angle_train %.3f angle_unsup %.3f '
                      '\n     head_train  %.3f head_unsup  %.3f '
                      '\n     left_arm_train  %.3f  left_arm_unsup  %.3f '
                      '\n     right_arm_train %.3f  right_arm_unsup %.3f '
                      '\n     left_leg_train  %.3f  left_leg_unsup  %.3f '
                      '\n     right_leg_train %.3f  right_leg_unsup %.3f '
                      '\n     symmetry_train  %.3f  symmetry_unsup %.3f '
                      '\n     2d_train_sup %f 2d_train_unsup %.3f 2d_valid %.3f' % (
                        epoch + 1,
                        elapsed,
                        lr,
                        losses_3d_train[-1] * 1000,
                        losses_3d_train_eval[-1] * 1000,
                        losses_3d_valid[-1] * 1000,
                        losses_traj_train_eval[-1] * 1000,
                        losses_traj_valid[-1] * 1000, 
                        losses_angle_train[-1] * 1000,
                        losses_angle_train_unlabeled[-1]* 1000,
                        losses_head_angle_train[-1]* 1000,
                        losses_head_angle_train_unlabeled[-1]* 1000,
                        losses_left_arm_angle_train[-1]* 1000,
                        losses_left_arm_angle_train_unlabeled[-1]* 1000,
                        losses_right_arm_angle_train[-1]* 1000,
                        losses_right_arm_angle_train_unlabeled[-1]* 1000,
                        losses_left_leg_angle_train[-1]* 1000,
                        losses_left_leg_angle_train_unlabeled[-1]* 1000,
                        losses_right_leg_angle_train[-1]* 1000,
                        losses_right_leg_angle_train_unlabeled[-1]* 1000,
                        losses_symmetry[-1], 
                        losses_symmetry_unlabeled[-1],
                        losses_2d_train_labeled_eval[-1],
                        losses_2d_train_unlabeled_eval[-1],
                        losses_2d_valid[-1]))
                """
                print('[%d] time %.2f lr %f 3d_train %.3f 3d_eval %.3f traj_eval %.3f 3d_valid %.3f '
                      'traj_valid %f 2d_train_sup %f 2d_train_unsup %.3f 2d_valid %.3f' % (
                        epoch + 1,
                        elapsed,
                        lr,
                        losses_3d_train[-1] * 1000,
                        losses_3d_train_eval[-1] * 1000,
                        losses_traj_train_eval[-1] * 1000,
                        losses_3d_valid[-1] * 1000,
                        losses_traj_valid[-1] * 1000,
                        losses_2d_train_labeled_eval[-1],
                        losses_2d_train_unlabeled_eval[-1],
                        losses_2d_valid[-1]))
                """
            else:
                print('[%d] time %.2f lr %f 3d_train %f 3d_eval %f 3d_valid %f' % (
                        epoch + 1,
                        elapsed,
                        lr,
                        losses_3d_train[-1] * 1000,
                        losses_3d_train_eval[-1] * 1000,
                        losses_3d_valid[-1]  *1000))
        
        # Decay learning rate exponentially
        lr *= lr_decay
        for param_group in optimizer.param_groups:
            param_group['lr'] *= lr_decay
        epoch += 1
        
        # Decay BatchNorm momentum
        momentum = initial_momentum * np.exp(-epoch/cfg.MODEL.EPOCHS * np.log(initial_momentum/final_momentum))
        model_pos_train.set_bn_momentum(momentum)
        if semi_supervised:
            model_traj_train.set_bn_momentum(momentum)
            
        # Save checkpoint if necessary
        if epoch % cfg.LOGS.CHECKPOINT_FREQUENCY == 0:
            chk_path = os.path.join(cfg.LOGS.CHECKPOINT, 'epoch_{}.bin'.format(epoch))
            print('Saving checkpoint to', chk_path)
            
            torch.save({
                'epoch': epoch,
                'lr': lr,
                'random_state': train_generator.random_state(),
                'optimizer': optimizer.state_dict(),
                'model_pos': model_pos_train.state_dict(),
                'model_traj': model_traj_train.state_dict() if semi_supervised else None,
                'random_state_semi': semi_generator.random_state() if semi_supervised else None,
            }, chk_path)
            
        # Save training curves after every epoch, as .png images (if requested)
        if cfg.LOGS.EXPORT_TRAINING_CURVES and epoch > 3:
            if 'matplotlib' not in sys.modules:
                import matplotlib
                matplotlib.use('Agg')
                import matplotlib.pyplot as plt
            
            plt.figure()
            epoch_x = np.arange(3, len(losses_3d_train)) + 1
            plt.plot(epoch_x, losses_3d_train[3:], '--', color='C0')
            plt.plot(epoch_x, losses_3d_train_eval[3:], color='C0')
            plt.plot(epoch_x, losses_3d_valid[3:], color='C1')
            plt.legend(['3d train', '3d train (eval)', '3d valid (eval)'])
            plt.ylabel('MPJPE (m)')
            plt.xlabel('Epoch')
            plt.xlim((3, epoch))
            plt.savefig(os.path.join(cfg.LOGS.CHECKPOINT, 'loss_3d.png'))

            if semi_supervised:
                plt.figure()
                plt.plot(epoch_x, losses_traj_train[3:], '--', color='C0')
                plt.plot(epoch_x, losses_traj_train_eval[3:], color='C0')
                plt.plot(epoch_x, losses_traj_valid[3:], color='C1')
                plt.legend(['traj. train', 'traj. train (eval)', 'traj. valid (eval)'])
                plt.ylabel('Mean distance (m)')
                plt.xlabel('Epoch')
                plt.xlim((3, epoch))
                plt.savefig(os.path.join(cfg.LOGS.CHECKPOINT, 'loss_traj.png'))

                plt.figure()
                plt.plot(epoch_x, losses_2d_train_labeled_eval[3:], color='C0')
                plt.plot(epoch_x, losses_2d_train_unlabeled[3:], '--', color='C1')
                plt.plot(epoch_x, losses_2d_train_unlabeled_eval[3:], color='C1')
                plt.plot(epoch_x, losses_2d_valid[3:], color='C2')
                plt.legend(['2d train labeled (eval)', '2d train unlabeled', '2d train unlabeled (eval)', '2d valid (eval)'])
                plt.ylabel('MPJPE (2D)')
                plt.xlabel('Epoch')
                plt.xlim((3, epoch))
                plt.savefig(os.path.join(cfg.LOGS.CHECKPOINT, 'loss_2d.png'))
                
                plt.figure()
                plt.plot(epoch_x, losses_angle_train[3:], '--', color='C0')
                plt.plot(epoch_x, losses_head_angle_train[3:], color='C0')
                plt.plot(epoch_x, losses_angle_train_unlabeled[3:], '--', color='C1')
                plt.plot(epoch_x, losses_head_angle_train_unlabeled[3:], color='C1')
                plt.legend(['Angle labeled ', 'HEAD Angle labeled', 'Angle unlabeled ', 'HEAD Angle unlabeled'])
                plt.ylabel('ANGLES')
                plt.xlabel('Epoch')
                plt.xlim((3, epoch))
                plt.savefig(os.path.join(cfg.LOGS.CHECKPOINT, 'head_angles.png'))
                
                plt.figure()
                plt.plot(epoch_x, losses_symmetry[3:], '--', color='C0')
                plt.plot(epoch_x, losses_symmetry_unlabeled[3:], '--', color='C1')
                plt.legend(['Symmetry labeled ',  'Symmetry unlabeled ', ])
                plt.ylabel('Symmetry')
                plt.xlabel('Epoch')
                plt.xlim((3, epoch))
                plt.savefig(os.path.join(cfg.LOGS.CHECKPOINT, 'symmetry.png'))
                
                plt.figure()
                plt.plot(epoch_x, losses_left_arm_angle_train[3:], '--', color='C0')
                plt.plot(epoch_x, losses_right_arm_angle_train[3:], color='C0')
                plt.plot(epoch_x, losses_left_arm_angle_train_unlabeled[3:], '--', color='C1')
                plt.plot(epoch_x, losses_right_arm_angle_train_unlabeled[3:], color='C1')
                plt.plot(epoch_x, losses_left_leg_angle_train[3:], '-', color='C0')
                plt.plot(epoch_x, losses_right_leg_angle_train[3:], 'o-', color='C0')
                plt.plot(epoch_x, losses_left_leg_angle_train_unlabeled[3:], '-', color='C1')
                plt.plot(epoch_x, losses_right_leg_angle_train_unlabeled[3:], 'o-', color='C1')
                plt.legend(['left arm Angle labeled ', 'Right arm Angle labeled', ' Left arm Angle unlabeled ', 'Right arm Angle unlabeled',
                            'left leg Angle labeled ', 'Right leg Angle labeled', ' Left leg Angle unlabeled ', 'Right leg Angle unlabeled'])
                plt.ylabel('ANGLES')
                plt.xlabel('Epoch')
                plt.xlim((3, epoch))
                plt.savefig(os.path.join(cfg.LOGS.CHECKPOINT, 'whole_angles.png'))
            plt.close('all')
    writer.close()

# Evaluate
def evaluate2(test_generator, action=None, return_predictions=False, use_trajectory_model=False):
    epoch_loss_3d_pos = 0
    epoch_loss_3d_pos_procrustes = 0
    epoch_loss_3d_pos_scale = 0
    epoch_loss_3d_vel = 0
    with torch.no_grad():
        if not use_trajectory_model:
            model_pos.eval()
        else:
            model_traj.eval()
        N = 0
        frame_id = 0
        for _, batch, batch_2d in test_generator.next_epoch():
            inputs_2d = torch.from_numpy(batch_2d.astype('float32'))
            if torch.cuda.is_available():
                inputs_2d = inputs_2d.cuda()

            # Positional model
            if not use_trajectory_model:
                predicted_3d_pos = model_pos(inputs_2d)
            else:
                predicted_3d_pos = model_traj(inputs_2d)

            # Test-time augmentation (if enabled)
            if test_generator.augment_enabled():
                # Undo flipping and take average with non-flipped version
                predicted_3d_pos[1, :, :, 0] *= -1
                if not use_trajectory_model:
                    predicted_3d_pos[1, :, joints_left + joints_right] = predicted_3d_pos[1, :, joints_right + joints_left]
                predicted_3d_pos = torch.mean(predicted_3d_pos, dim=0, keepdim=True)
                
            if return_predictions:
                return predicted_3d_pos.squeeze(0).cpu().numpy()
                
            inputs_3d = torch.from_numpy(batch.astype('float32'))
            if torch.cuda.is_available():
                inputs_3d = inputs_3d.cuda()
            inputs_3d[:, :, 0] = 0    
            if test_generator.augment_enabled():
                inputs_3d = inputs_3d[:1]

            error = mpjpe(predicted_3d_pos, inputs_3d, mode='eval')
            epoch_loss_3d_pos_scale += inputs_3d.shape[0]*inputs_3d.shape[1] * n_mpjpe(predicted_3d_pos, inputs_3d, mode='eval').item()

            m2mm = 1000
            THRESHOLD = 150
            if error.item()*m2mm > THRESHOLD:
                print("Problem detected on Frame {} with inputs shape {}".format(frame_id, inputs_3d.shape))
            epoch_loss_3d_pos += inputs_3d.shape[0]*inputs_3d.shape[1] * error.item()
            N += inputs_3d.shape[0] * inputs_3d.shape[1]
            
            inputs = inputs_3d.cpu().numpy().reshape(-1, inputs_3d.shape[-2], inputs_3d.shape[-1])
            predicted_3d_pos = predicted_3d_pos.cpu().numpy().reshape(-1, inputs_3d.shape[-2], inputs_3d.shape[-1])

            epoch_loss_3d_pos_procrustes += inputs_3d.shape[0]*inputs_3d.shape[1] * p_mpjpe(predicted_3d_pos, inputs, mode='eval')

            # Compute velocity error
            epoch_loss_3d_vel += inputs_3d.shape[0]*inputs_3d.shape[1] * mean_velocity_error(predicted_3d_pos, inputs, mode='eval')
    """        
    if action is None:
        print('----------')
    else:
        print('----'+action+'----')
    """
    e1 = (epoch_loss_3d_pos / N)*1000
    e2 = (epoch_loss_3d_pos_procrustes / N)*1000
    e3 = (epoch_loss_3d_pos_scale / N)*1000
    ev = (epoch_loss_3d_vel / N)*1000
    """
    print('Test time augmentation:', test_generator.augment_enabled())
    print('Protocol #1 Error (MPJPE):', e1, 'mm')
    print('Protocol #2 Error (P-MPJPE):', e2, 'mm')
    print('Protocol #3 Error (N-MPJPE):', e3, 'mm')
    print('Velocity Error (MPJVE):', ev, 'mm')
    print('----------')
    """
    return e1, e2, e3, ev



# Evaluate
def evaluate(test_generator, action=None, return_predictions=False, use_trajectory_model=False, nruns=cfg.TRAIN.NRUNS):
    epoch_loss_3d_pos = []
    epoch_loss_3d_pos_procrustes = []
    epoch_loss_3d_pos_scale = []
    epoch_loss_3d_vel = []

    for run in range(nruns): 
        fixseed(run)
        e1, e2, e3, e4 = evaluate2(test_generator, action, return_predictions, use_trajectory_model)
        epoch_loss_3d_pos.append(e1)
        epoch_loss_3d_pos_procrustes.append(e2)
        epoch_loss_3d_pos_scale.append(e3)
        epoch_loss_3d_vel.append(e4)
                

        print('------ Action {}: Global Evaluation on {} Runs ------'.format(action, nruns))
        e1, var_e1 = np.mean(epoch_loss_3d_pos), np.std(epoch_loss_3d_pos)
        e2, var_e2 = np.mean(epoch_loss_3d_pos_procrustes), np.std(epoch_loss_3d_pos_procrustes)
        e3, var_e3 = np.mean(epoch_loss_3d_pos_scale), np.std(epoch_loss_3d_pos_scale)
        ev, var_ev = np.mean(epoch_loss_3d_vel), np.std(epoch_loss_3d_vel)
        print('Test time augmentation:', test_generator.augment_enabled())
        print('Protocol #1 Error (MPJPE): {} +/- {} mm'.format(e1, var_e1))
        print('Protocol #2 Error (P-MPJPE): {} +/- {} mm'.format(e2, var_e2))
        print('Protocol #3 Error (N-MPJPE): {} +/- {} mm'.format(e3, var_e3))
        print('Velocity Error (MPJVE): {} +/- {} mm'.format(ev, var_ev))
        print('----------')

    return e1, e2, e3, ev


if cfg.VIS.RENDER:
    print('Rendering...')
    
    input_keypoints = keypoints[cfg.VIS.SUBJECT][cfg.VIS.ACTION].copy() # removed last key [cfg.VIS.CAMERA]
    ground_truth = None
    if cfg.VIS.SUBJECT in dataset.subjects() and cfg.VIS.ACTION in dataset[cfg.VIS.SUBJECT]:
        if 'positions_3d' in dataset[cfg.VIS.SUBJECT][cfg.VIS.ACTION]:
            ground_truth = dataset[cfg.VIS.SUBJECT][cfg.VIS.ACTION]['positions_3d'].copy() # removed last key [cfg.VIS.CAMERA]
            # Added
            ground_truth =  np.reshape(ground_truth[0], (len(ground_truth[0]), 17, 3))
    if ground_truth is None:
        print('INFO: this action is unlabeled. Ground truth will not be rendered.')
        
    gen = UnchunkedGenerator(None, None, [input_keypoints],
                             pad=pad, causal_shift=causal_shift, augment=cfg.MODEL.TEST_TIME_AUGMENTATION,
                             kps_left=kps_left, kps_right=kps_right, joints_left=joints_left, joints_right=joints_right)
    prediction = evaluate(gen, return_predictions=True)
    if model_traj is not None and ground_truth is None:
        prediction_traj = evaluate(gen, return_predictions=True, use_trajectory_model=True)
        prediction += prediction_traj
    
    if cfg.VIS.EXPORT is not None:
        print('Exporting joint positions to', cfg.VIS.EXPORT)
        # Predictions are in camera space
        np.save(cfg.VIS.EXPORT, prediction)
    
    if cfg.VIS.OUTPUT is not None:
        if ground_truth is not None:
            # Reapply trajectory
            trajectory = ground_truth[:, :1]
            # ground_truth[:, 1:] += trajectory
            ground_truth[:, :] += trajectory # Add trajectory to root
            prediction += trajectory
        
        """
        # Invert camera transformation
        cam = dataset.cameras()[cfg.VIS.SUBJECT][cfg.VIS.CAMERA]
        if ground_truth is not None:
            prediction = camera_to_world(prediction, R=cam['orientation'], t=cam['translation'])
            ground_truth = camera_to_world(ground_truth, R=cam['orientation'], t=cam['translation'])
        else:
            # If the ground truth is not available, take the camera extrinsic params from a random subject.
            # They are almost the same, and anyway, we only need this for visualization purposes.
            for subject in dataset.cameras():
                if 'orientation' in dataset.cameras()[subject][cfg.VIS.CAMERA]:
                    rot = dataset.cameras()[subject][cfg.VIS.CAMERA]['orientation']
                    break
            prediction = camera_to_world(prediction, R=rot, t=0)
            # We don't have the trajectory, but at least we can rebase the height
            prediction[:, :, 2] -= np.min(prediction[:, :, 2])
        """
        anim_output = {'Reconstruction': prediction}
        if ground_truth is not None and not cfg.VIS.NO_GT:
            anim_output['Ground truth'] = ground_truth
        
        input_keypoints = image_coordinates(input_keypoints[..., :2], w=cam['res_w'], h=cam['res_h'])
        
        from common.visualization import render_animation
        render_animation(input_keypoints, keypoints_metadata, anim_output,
                         dataset.skeleton(), dataset.fps(), cfg.VIS.BITRATE, cfg.VIS.AZIM, cfg.VIS.OUTPUT,
                         limit=cfg.VIS.FRAME_LIMIT, downsample=cfg.VIS.DOWNSAMPLE, size=cfg.VIS.SIZE,
                         input_video_path=cfg.VIS.VIDEO, viewport=(cam['res_w'], cam['res_h']),
                         input_video_skip=cfg.VIS.SKIP)
    
else:
    print('Evaluating...')
    all_actions = {}
    all_actions_by_subject = {}
    for subject in subjects_test:
        if subject not in all_actions_by_subject:
            all_actions_by_subject[subject] = {}

        for action in dataset[subject].keys():
            action_name = action.split(' ')[0]
            if action_name not in all_actions:
                all_actions[action_name] = []
            if action_name not in all_actions_by_subject[subject]:
                all_actions_by_subject[subject][action_name] = []
            all_actions[action_name].append((subject, action))
            all_actions_by_subject[subject][action_name].append((subject, action))

    def fetch_actions(actions):
        out_poses_3d = []
        out_poses_2d = []

        for subject, action in actions:
            poses_2d = np.array([keypoints[subject][action]])
            
            for i in range(len(poses_2d)): # Iterate across cameras
                out_poses_2d.append(poses_2d[i][cfg.LOGS.SEQ_START: cfg.LOGS.SEQ_START + cfg.LOGS.SEQ_LENGTH])

            poses_3d = dataset[subject][action]['positions_3d']
            assert len(poses_3d) == len(poses_2d), 'Camera count mismatch'
            for i in range(len(poses_3d)): # Iterate across cameras
                pose_3d = poses_3d[i][cfg.LOGS.SEQ_START: cfg.LOGS.SEQ_START + cfg.LOGS.SEQ_LENGTH]
                pose_3d = np.reshape(pose_3d, (len(pose_3d), 17, 3))
                out_poses_3d.append(pose_3d)
    

        stride = cfg.EXPS.DOWNSAMPLE
        if stride > 1:
            # Downsample as requested
            for i in range(len(out_poses_2d)):
                out_poses_2d[i] = out_poses_2d[i][::stride]
                if out_poses_3d is not None:
                    out_poses_3d[i] = out_poses_3d[i][::stride]
        
        return out_poses_3d, out_poses_2d

    def run_evaluation(actions, action_filter=None, nruns=cfg.TRAIN.NRUNS):
        errors_p1 = []
        errors_p2 = []
        errors_p3 = []
        errors_vel = []
        import sys
        
        for action_key in actions.keys():
            errors_p1_i = []
            errors_p2_i = []
            errors_p3_i = []
            errors_vel_i = []
            for i in range(nruns):
                sys.stdout.write("\n        Running Evaluation ..... {} / {}".format(i + 1, nruns))
                fixseed(i)
                if action_filter is not None:
                    found = False
                    for a in action_filter:
                        if action_key.startswith(a):
                            found = True
                            break
                    if not found:
                        continue

                poses_act, poses_2d_act = fetch_actions(actions[action_key])
                gen = UnchunkedGenerator(None, poses_act, poses_2d_act,
                                        pad=pad, causal_shift=causal_shift, augment=cfg.MODEL.TEST_TIME_AUGMENTATION,
                                        kps_left=kps_left, kps_right=kps_right, joints_left=joints_left, joints_right=joints_right)
                e1, e2, e3, ev = evaluate2(gen, action_key)
                errors_p1_i.append(e1)
                errors_p2_i.append(e2)
                errors_p3_i.append(e3)
                errors_vel_i.append(ev)
            e1, var_e1 = np.mean(errors_p1_i), np.std(errors_p1_i)
            e2, var_e2 = np.mean(errors_p2_i), np.std(errors_p2_i)
            e3, var_e3 = np.mean(errors_p3_i), np.std(errors_p3_i)
            ev, var_ev = np.mean(errors_vel_i), np.std(errors_vel_i)
            errors_p1.append(e1)
            errors_p2.append(e2)
            errors_p3.append(e3)
            errors_vel.append(ev)
            
            print('------ Action {}: Eval on {} Runs ------'.format(action, nruns))
            print('Test time augmentation:', test_generator.augment_enabled())
            print('Protocol #1 Error (MPJPE): {} +/- {} mm'.format(round(e1, 1), round(var_e1, 1)))
            print('Protocol #2 Error (P-MPJPE): {} +/- {} mm'.format(round(e2, 1), round(var_e2, 1)))
            print('Protocol #3 Error (N-MPJPE): {} +/- {} mm'.format(round(e3, 1), round(var_e3, 1)))
            print('Velocity Error (MPJVE): {} +/- {} mm'.format(round(ev, 1), round(var_ev, 1)))
            print('----------\n')

        print()
        print('Protocol #1   (MPJPE) action-wise average:', round(np.mean(errors_p1), 1), 'mm')
        print('Protocol #2 (P-MPJPE) action-wise average:', round(np.mean(errors_p2), 1), 'mm')
        print('Protocol #3 (N-MPJPE) action-wise average:', round(np.mean(errors_p3), 1), 'mm')
        print('Velocity      (MPJVE) action-wise average:', round(np.mean(errors_vel), 2), 'mm')

    if not cfg.TRAIN.BY_SUBJECT:
        run_evaluation(all_actions, action_filter)
    else:
        for subject in all_actions_by_subject.keys():
            print('Evaluating on subject', subject)
            run_evaluation(all_actions_by_subject[subject], action_filter)
            print('')