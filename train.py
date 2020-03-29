# Copyright 2020 Lorna Authors. All Rights Reserved.
# Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
import argparse
import glob
import math
import os
import random
import time
import warnings

import numpy as np
import torch.distributed as dist
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.data
from tqdm import tqdm

from easydet.config import parse_data_config
from easydet.data import LoadImagesAndLabels
from easydet.solver import ModelEMA
from easydet.utils import init_seeds
from easydet.utils import labels_to_class_weights
from easydet.utils import labels_to_image_weights
from easydet.utils import select_device
from models import Darknet
from models import load_darknet_weights
from test import evaluate
from utils import compute_loss
from utils import fitness
from utils import plot_results
from utils import print_mutation

mixed_precision = True
try:  # Mixed precision training https://github.com/NVIDIA/apex
    from apex import amp
except:
    mixed_precision = False  # not installed

parameters = {"giou": 3.54,  # giou loss gain
              "cls": 37.4,  # cls loss gain
              "cls_pw": 1.0,  # cls BCELoss positive_weight
              "obj": 64.3,  # obj loss gain (*=img_size/320 if img_size != 320)
              "obj_pw": 1.0,  # obj BCELoss positive_weight
              "iou_t": 0.225,  # iou training threshold
              "lr0": 0.01,  # initial learning rate (SGD=5E-3, Adam=5E-4)
              "lrf": 0.0005,  # final learning rate (with cos scheduler)
              "momentum": 0.937,  # SGD momentum
              "weight_decay": 0.000484,  # optimizer weight decay
              "fl_gamma": 0.0,  # focal loss gamma (default is gamma=1.5)
              "hsv_h": 0.0138,  # image HSV-Hue augmentation (fraction)
              "hsv_s": 0.678,  # image HSV-Saturation augmentation (fraction)
              "hsv_v": 0.36,  # image HSV-Value augmentation (fraction)
              "degrees": 1.98 * 0,  # image rotation (+/- deg)
              "translate": 0.05 * 0,  # image translation (+/- fraction)
              "scale": 0.05 * 0,  # image scale (+/- gain)
              "shear": 0.641 * 0}  # image shear (+/- deg)

# Overwrite hyp with hyp*.txt
parameter_file = glob.glob("hyp*.txt")
if parameter_file:
    print(f"Using {parameter_file[0]}")
    for keys, value in zip(parameters.keys(), np.loadtxt(parameter_file[0])):
        parameters[keys] = value


def train():
    cfg = args.cfg
    data = args.data
    if len(args.image_size) == 2:
        image_size, image_size_val = args.image_size[0], args.image_size[1]
    else:
        image_size, image_size_val = args.image_size[0], args.image_size[0]

    epochs = args.epochs
    batch_size = args.batch_size
    accumulate = args.accumulate
    weights = args.weights

    # Initialize
    init_seeds()
    image_size_min = 6.6  # 320 / 32 / 1.5
    image_size_max = 28.5  # 320 / 32 / 28.5
    if args.multi_scale:
        image_size_min = round(image_size / 32 / 1.5)
        image_size_max = round(image_size / 32 * 1.5)
        image_size = image_size_max * 32  # initiate with maximum multi_scale size
        print(f"Using multi-scale {image_size_min * 32} - {image_size}")

    # Configure run
    dataset_dict = parse_data_config(data)
    train_path = dataset_dict["train"]
    valid_path = dataset_dict["valid"]
    num_classes = 1 if args.single_cls else int(dataset_dict["classes"])

    # Remove previous results
    for files in glob.glob("results.txt"):
        os.remove(files)

    # Initialize model
    model = Darknet(cfg).to(device)

    # Optimizer
    pg0, pg1, pg2 = [], [], []  # optimizer parameter groups
    for model_key, model_value in dict(model.named_parameters()).items():
        if ".bias" in model_key:
            pg2 += [model_value]  # biases
        elif "Conv2d.weight" in model_key:
            pg1 += [model_value]  # apply weight_decay
        else:
            pg0 += [model_value]  # all else

    optimizer = torch.optim.SGD(pg0,
                                lr=parameters["lr0"],
                                momentum=parameters["momentum"],
                                nesterov=True)
    optimizer.add_param_group({"params": pg1,
                               # add pg1 with weight_decay
                               "weight_decay": parameters["weight_decay"]})
    optimizer.add_param_group({"params": pg2})  # add pg2 with biases
    del pg0, pg1, pg2

    epoch = 0
    start_epoch = 0
    best_fitness = 0.0
    context = None
    if weights.endswith(".pth"):
        state = torch.load(weights, map_location=device)
        # load model
        try:
            state["model"] = {k: v for k, v in state["model"].items()
                              if model.state_dict()[k].numel() == v.numel()}
            model.load_state_dict(state["model"], strict=False)
        except KeyError as e:
            error_msg = f"{args.weights} is not compatible with {args.cfg}. "
            error_msg += f"Specify --weights `` or specify a --cfg "
            error_msg += f"compatible with {args.weights}. "
            raise KeyError(error_msg) from e

        # load optimizer
        if state["optimizer"] is not None:
            optimizer.load_state_dict(state["optimizer"])
            best_fitness = state["best_fitness"]

        # load results
        if state.get("training_results") is not None:
            with open("results.txt", "w") as file:
                file.write(state["training_results"])  # write results.txt

        start_epoch = state["epoch"] + 1
        del state

    elif len(weights) > 0:
        # possible weights are "*.weights", "yolov3-tiny.conv.15",  "darknet53.conv.74" etc.
        load_darknet_weights(model, weights)
    else:
        print("Pre training model weight not loaded.")

    # Mixed precision training https://github.com/NVIDIA/apex
    if mixed_precision:
        # skip print amp info
        model, optimizer = amp.initialize(model, optimizer, opt_level="O1", verbosity=0)
    # source https://arxiv.org/pdf/1812.01187.pdf
    lr_lambda = lambda x: (((1 + math.cos(x * math.pi / epochs)) / 2) ** 1.0) * 0.95 + 0.05
    scheduler = torch.optim.lr_scheduler.LambdaLR(optimizer,
                                                  lr_lambda=lr_lambda,
                                                  last_epoch=start_epoch - 1)

    # Initialize distributed training
    if device.type != "cpu" and torch.cuda.device_count() > 1 and torch.distributed.is_available():
        dist.init_process_group(backend="nccl",  # "distributed backend"
                                # distributed training init method
                                init_method="tcp://127.0.0.1:9999",
                                # number of nodes for distributed training
                                world_size=1,
                                # distributed training node rank
                                rank=0)
        model = torch.nn.parallel.DistributedDataParallel(model)
        model.yolo_layers = model.module.yolo_layers

    # Dataset
    # Apply augmentation hyperparameters (option: rectangular training)
    train_dataset = LoadImagesAndLabels(train_path, image_size, batch_size,
                                        augment=True,
                                        hyp=parameters,
                                        rect=args.rect,
                                        cache_images=args.cache_images,
                                        single_cls=args.single_cls)
    # No apply augmentation hyperparameters and rectangular inference
    valid_dataset = LoadImagesAndLabels(valid_path, image_size_val,
                                        batch_size * 2,
                                        augment=False,
                                        hyp=parameters,
                                        rect=True,
                                        cache_images=args.cache_images,
                                        single_cls=args.single_cls)
    collate_fn = train_dataset.collate_fn
    # Dataloader
    train_dataloader = torch.utils.data.DataLoader(train_dataset,
                                                   batch_size=batch_size,
                                                   num_workers=args.workers,
                                                   shuffle=not args.rect,
                                                   pin_memory=True,
                                                   collate_fn=collate_fn)
    valid_dataloader = torch.utils.data.DataLoader(valid_dataset,
                                                   batch_size=batch_size * 2,
                                                   num_workers=args.workers,
                                                   shuffle=False,
                                                   pin_memory=True,
                                                   collate_fn=collate_fn)

    # Model parameters
    model.nc = num_classes  # attach number of classes to model
    model.hyp = parameters  # attach hyperparameters to model
    model.gr = 0.0  # giou loss ratio (obj_loss = 1.0 or giou)
    # attach class weights
    model.class_weights = labels_to_class_weights(train_dataset.labels, num_classes).to(device)

    # Model EMA
    ema = ModelEMA(model, decay=0.9998)

    # Start training
    batches_num = len(train_dataloader)  # number of batches
    prebias = start_epoch == 0
    maps = np.zeros(num_classes)  # mAP per class
    # "P", "R", "mAP", "F1", "val GIoU", "val Objectness", "val Classification"
    results = (0, 0, 0, 0, 0, 0, 0)
    print(f"Using {args.workers} dataloader workers.")
    print(f"Starting training for {args.epochs} epochs...")

    start_time = time.time()
    for epoch in range(start_epoch, args.epochs):
        model.train()

        # Prebias
        if prebias:
            warmup_epoch = 3  # number of prebias epochs
            warmup_parameter = 0.1, 0.9  # (lr=0.1, momentum=0.9)

            if epoch == warmup_epoch:
                # normal training settings
                warmup_parameter = parameters["lr0"], parameters["momentum"]
                model.gr = 1.0  # giou loss ratio (obj_loss = giou)
                prebias = False

            # Bias optimizer settings
            optimizer.param_groups[2]["lr"] = warmup_parameter[0]
            if optimizer.param_groups[2].get("momentum") is not None:
                optimizer.param_groups[2]["momentum"] = warmup_parameter[1]

        # Update image weights (optional)
        if train_dataset.image_weights:
            class_weights = model.class_weights.cpu().numpy() * (
                    1 - maps) ** 2  # class weights
            image_weights = labels_to_image_weights(train_dataset.labels,
                                                    num_classes=num_classes,
                                                    class_weights=class_weights)
            # rand weighted index
            train_dataset.indices = random.choices(range(train_dataset.image_files_num),
                                                   weights=image_weights,
                                                   k=train_dataset.image_files_num)

        mean_losses = torch.zeros(4).to(device)
        print("\n")
        print(("%10s" * 8) % ("Epoch", "memory", "GIoU", "obj", "cls", "total", "targets",
                              " image_size"))
        progress_bar = tqdm(enumerate(train_dataloader), total=batches_num)
        for index, (images, targets, paths, _) in progress_bar:
            # number integrated batches (since train start)
            ni = index + batches_num * epoch
            # uint8 to float32, 0 - 255 to 0.0 - 1.0
            images = images.to(device).float() / 255.0
            targets = targets.to(device)

            # Hyper parameter Burn-in
            n_burn = 200  # number of burn-in batches
            if ni <= n_burn:
                for m in model.named_modules():
                    if m[0].endswith("BatchNorm2d"):
                        m[1].track_running_stats = ni == n_burn

            # Multi-Scale training
            if args.multi_scale:
                #  adjust img_size (67% - 150%) every 1 batch
                if ni / accumulate % 1 == 0:
                    image_size = random.randrange(image_size_min, image_size_max + 1) * 32
                scale_ratio = image_size / max(images.shape[2:])
                if scale_ratio != 1:
                    # new shape (stretched to 32-multiple)
                    new_size = [math.ceil(size * scale_ratio / 32.) * 32
                                for size in images.shape[2:]]
                    images = F.interpolate(images,
                                           size=new_size,
                                           mode="bilinear",
                                           align_corners=False)

            # Run model
            output = model(images)

            # Compute loss
            loss, loss_items = compute_loss(output, targets, model)
            if not torch.isfinite(loss):
                warnings.warn(f"WARNING: Non-finite loss, ending training {loss_items}")
                return results

            # Scale loss by nominal batch_size of 64
            loss *= batch_size / 64

            # Compute gradient
            if mixed_precision:
                with amp.scale_loss(loss, optimizer) as scaled_loss:
                    scaled_loss.backward()
            else:
                loss.backward()

            # Optimize accumulated gradient
            if ni % accumulate == 0:
                optimizer.step()
                optimizer.zero_grad()
                ema.update(model)

            # Print batch results
            # update mean losses
            mean_losses = (mean_losses * index + loss_items) / (index + 1)
            memory = f"{torch.cuda.memory_cached() / 1E9 if torch.cuda.is_available() else 0:.2f}G"
            context = ("%10s" * 2 + "%10.3g" * 6) % (
                "%g/%g" % (epoch, args.epochs - 1),
                memory, *mean_losses,
                len(targets), image_size)
            progress_bar.set_description(context)

        # Update scheduler
        scheduler.step()

        # Process epoch results
        ema.update_attr(model)
        final_epoch = epoch + 1 == epochs
        if not args.notest or final_epoch:  # Calculate mAP
            coco = any([coco_name in data for coco_name in ["coco.data",
                                                            "coco2014.data",
                                                            "coco2017.data"]]) and model.nc == 80
            results, maps = evaluate(cfg,
                                     data,
                                     batch_size=batch_size * 2,
                                     image_size=image_size_val,
                                     model=ema.ema,
                                     confidence_threshold=0.001 if final_epoch else 0.01,
                                     iou_threshold=0.6,
                                     save_json=final_epoch and coco,
                                     single_cls=args.single_cls,
                                     dataloader=valid_dataloader)

        # Write epoch results
        with open("results.txt", "a") as f:
            # P, R, mAP, F1, test_losses=(GIoU, obj, cls)
            f.write(context + "%10.3g" * 7 % results)
            f.write("\n")

        # Write Tensorboard results
        if tb_writer:
            titles = ["GIoU", "Objectness", "Classification", "Train loss",
                      "Precision", "Recall", "mAP", "F1", "val GIoU",
                      "val Objectness", "val Classification"]
            for xi, title in zip(list(mean_losses) + list(results), titles):
                tb_writer.add_scalar(title, xi, epoch)

        # Update best mAP
        # fitness_i = weighted combination of [P, R, mAP, F1]
        fitness_i = fitness(np.array(results).reshape(1, -1))
        if fitness_i > best_fitness:
            best_fitness = fitness_i

        # Save training results
        save = (not args.nosave) or (final_epoch and not args.evolve)
        if save:
            with open("results.txt", "r") as f:
                # Create checkpoint
                state = {"epoch": epoch,
                         "best_fitness": best_fitness,
                         "training_results": f.read(),
                         "model": ema.ema.module.state_dict()
                         if hasattr(model, 'module') else ema.ema.state_dict(),
                         "optimizer": None
                         if final_epoch else optimizer.state_dict()}

        # Save last checkpoint
        torch.save(state, "weights/checkpoint.pth")

        # Save best checkpoint
        if best_fitness == fitness_i:
            state = {"epoch": -1,
                     "best_fitness": None,
                     "training_results": None,
                     "model": model.state_dict(),
                     "optimizer": None}
            torch.save(state, "weights/model_best.pth")

        # Delete checkpoint
        del state

    if not args.evolve:
        plot_results()  # save as results.png
    print(f"{epoch - start_epoch} epochs completed "
          f"in "f"{(time.time() - start_time) / 3600:.3f} hours.\n")
    dist.destroy_process_group() if torch.cuda.device_count() > 1 else None
    torch.cuda.empty_cache()

    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--epochs", type=int, default=273,
                        help="500200 is yolov3 max batches. (default: 237)"
                             "Formula: Epochs = 500200 / (117263 / 64).")
    parser.add_argument("--batch-size", type=int, default=16,
                        help="mini-batch size (default: 16), this is the total "
                             "batch size of all GPUs on the current node when "
                             "using Data Parallel or Distributed Data Parallel"
                             "Effective batch size is batch_size * accumulate.")
    parser.add_argument("--accumulate", type=int, default=4,
                        help="Batches to accumulate before optimizing. "
                             "(default: 4)")
    parser.add_argument("--cfg", type=str, default="cfgs/yolov3.cfg",
                        help="Neural network profile path. "
                             "(default: cfgs/yolov3.cfg)")
    parser.add_argument("--data", type=str, default="data/coco2014.data",
                        help="Path to dataset. (default: cfgs/coco2014.data)")
    parser.add_argument("--workers", default=4, type=int, metavar="N",
                        help="Number of data loading workers (default: 4)")
    parser.add_argument("--multi-scale", action="store_true",
                        help="adjust (67% - 150%) img_size every 10 batches")
    parser.add_argument("--image-size", nargs="+", type=int, default=[416],
                        help="Size of processing picture. (default: [416])")
    parser.add_argument("--rect", action="store_true",
                        help="rectangular training for faster training.")
    parser.add_argument("--resume", action="store_true",
                        help="resume training from checkpoint.pth")
    parser.add_argument("--nosave", action="store_true",
                        help="only save final checkpoint")
    parser.add_argument("--notest", action="store_true",
                        help="only test final epoch")
    parser.add_argument("--evolve", action="store_true",
                        help="evolve hyperparameters")
    parser.add_argument("--cache-images", action="store_true",
                        help="cache images for faster training.")
    parser.add_argument("--weights", type=str, default="",
                        help="Model file weight path. (default: ``)")
    parser.add_argument("--device", default="",
                        help="device id (i.e. 0 or 0,1 or cpu)")
    parser.add_argument("--single-cls", action="store_true",
                        help="train as single-class dataset")
    args = parser.parse_args()
    args.weights = "weights/checkpoint.pth" if args.resume else args.weights

    print(args)

    device = select_device(args.device, apex=mixed_precision,
                           batch_size=args.batch_size)
    if device.type == "cpu":
        mixed_precision = False

    try:
        os.makedirs("weights")
    except OSError:
        pass

    tb_writer = None
    if not args.evolve:
        try:
            # Start Tensorboard with "tensorboard --logdir=runs"
            from torch.utils.tensorboard import SummaryWriter

            tb_writer = SummaryWriter()
        except:
            pass

        train()

    else:  # Evolve hyperparameters (optional)
        args.notest, args.nosave = True, True  # only test/save final epoch

        for _ in range(1):  # generations to evolve
            # if evolve.txt exists: select best hyps and mutate
            if os.path.exists("evolve.txt"):
                # Select parent(s)
                parent = "single"  # parent selection method: "single" or "weighted"
                x = np.loadtxt("evolve.txt", ndmin=2)
                n = min(5, len(x))  # number of previous results to consider
                x = x[np.argsort(-fitness(x))][:n]  # top n mutations
                w = fitness(x) - fitness(x).min()  # weights
                if parent == "single" or len(x) == 1:
                    x = x[random.choices(range(n), weights=w)[
                        0]]  # weighted selection
                elif parent == "weighted":
                    x = (x * w.reshape(n, 1)).sum(
                        0) / w.sum()  # weighted combination

                # Mutate
                method, mp, s = 3, 0.9, 0.2  # method, mutation probability, sigma
                np.random.seed(int(time.time()))
                # gains
                g = np.array(
                    [1, 1, 1, 1, 1, 1, 1, 0, .1, 1, 0, 1, 1, 1, 1, 1, 1, 1])
                ng = len(g)
                if method == 1:
                    v = (np.random.randn(ng) *
                         np.random.random() * g * s + 1) ** 2.0
                elif method == 2:
                    v = (np.random.randn(ng) *
                         np.random.random(ng) * g * s + 1) ** 2.0
                elif method == 3:
                    v = np.ones(ng)
                    # mutate until a change occurs (prevent duplicates)
                    while all(v == 1):
                        v = (g * (np.random.random(ng) < mp) *
                             np.random.randn(ng) *
                             np.random.random() * s + 1).clip(0.3, 3.0)
                for i, k in enumerate(
                        parameters.keys()):  # plt.hist(v.ravel(), 300)
                    parameters[k] = x[i + 7] * v[i]  # mutate

            # Clip to limits
            keys = ["lr0", "iou_t", "momentum", "weight_decay", "hsv_s",
                    "hsv_v", "translate", "scale", "fl_gamma"]
            limits = [(1e-5, 1e-2), (0.00, 0.70), (0.60, 0.98), (0, 0.001),
                      (0, .9), (0, .9), (0, .9), (0, .9), (0, 3)]
            for k, v in zip(keys, limits):
                parameters[k] = np.clip(parameters[k], v[0], v[1])

            # Train mutation
            res = train()

            # Write mutation results
            print_mutation(parameters, res)
