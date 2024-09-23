#!/usr/bin/env python3
# -*- coding:utf-8 -*-
# Copyright (c) Megvii, Inc. and its affiliates.
###########################################################################
# Copyright (C) 2022 Habana Labs, Ltd. an Intel Company
###########################################################################

import contextlib
import io
import itertools
import json
import tempfile
import time
from loguru import logger
from tabulate import tabulate
from tqdm import tqdm

import numpy as np

import torch

from yolox.data.datasets import COCO_CLASSES
from yolox.utils import (
    gather,
    is_main_process,
    Postprocessor,
    synchronize,
    time_synchronized,
    xyxy2xywh
)


def per_class_AR_table(coco_eval, class_names=COCO_CLASSES, headers=["class", "AR"], colums=6):
    per_class_AR = {}
    recalls = coco_eval.eval["recall"]
    # dimension of recalls: [TxKxAxM]
    # recall has dims (iou, cls, area range, max dets)
    assert len(class_names) == recalls.shape[1]

    for idx, name in enumerate(class_names):
        recall = recalls[:, idx, 0, -1]
        recall = recall[recall > -1]
        ar = np.mean(recall) if recall.size else float("nan")
        per_class_AR[name] = float(ar * 100)

    num_cols = min(colums, len(per_class_AR) * len(headers))
    result_pair = [x for pair in per_class_AR.items() for x in pair]
    row_pair = itertools.zip_longest(*[result_pair[i::num_cols] for i in range(num_cols)])
    table_headers = headers * (num_cols // len(headers))
    table = tabulate(
        row_pair, tablefmt="pipe", floatfmt=".3f", headers=table_headers, numalign="left",
    )
    return table


def per_class_AP_table(coco_eval, class_names=COCO_CLASSES, headers=["class", "AP"], colums=6):
    per_class_AP = {}
    precisions = coco_eval.eval["precision"]
    # dimension of precisions: [TxRxKxAxM]
    # precision has dims (iou, recall, cls, area range, max dets)
    assert len(class_names) == precisions.shape[2]

    for idx, name in enumerate(class_names):
        # area range index 0: all area ranges
        # max dets index -1: typically 100 per image
        precision = precisions[:, :, idx, 0, -1]
        precision = precision[precision > -1]
        ap = np.mean(precision) if precision.size else float("nan")
        per_class_AP[name] = float(ap * 100)

    num_cols = min(colums, len(per_class_AP) * len(headers))
    result_pair = [x for pair in per_class_AP.items() for x in pair]
    row_pair = itertools.zip_longest(*[result_pair[i::num_cols] for i in range(num_cols)])
    table_headers = headers * (num_cols // len(headers))
    table = tabulate(
        row_pair, tablefmt="pipe", floatfmt=".3f", headers=table_headers, numalign="left",
    )
    return table


class COCOEvaluator:
    """
    COCO AP Evaluation class.  All the data in the val2017 dataset are processed
    and evaluated by COCO API.
    """

    def __init__(
        self,
        dataloader,
        img_size: int,
        confthre: float,
        nmsthre: float,
        num_classes: int,
        testdev: bool = False,
        per_class_AP: bool = False,
        per_class_AR: bool = False,
        use_hpu: bool = False,
        warmup_steps: int = 0,
        cpu_post_processing: bool = False
    ):
        """
        Args:
            dataloader (Dataloader): evaluate dataloader.
            img_size: image size after preprocess. images are resized
                to squares whose shape is (img_size, img_size).
            confthre: confidence threshold ranging from 0 to 1, which
                is defined in the config file.
            nmsthre: IoU threshold of non-max supression ranging from 0 to 1.
            per_class_AP: Show per class AP during evalution or not. Default to False.
            per_class_AR: Show per class AR during evalution or not. Default to False.
        """
        self.dataloader = dataloader
        self.img_size = img_size
        self.confthre = confthre
        self.nmsthre = nmsthre
        self.num_classes = num_classes
        self.testdev = testdev
        self.per_class_AP = per_class_AP
        self.per_class_AR = per_class_AR
        self.use_hpu = use_hpu
        self.cpu_post_processing = cpu_post_processing
        self.warmup_steps = warmup_steps

        self.post_proc_device = None
        if self.cpu_post_processing:
            self.post_proc_device = "cpu"
        elif self.use_hpu:
            self.post_proc_device = "hpu"

        self.postprocessor = Postprocessor(
                                    self.num_classes,
                                    self.confthre,
                                    self.nmsthre,
                                    self.post_proc_device
                            )
        if self.cpu_post_processing:
            self.postprocessor = torch.jit.script(self.postprocessor)

    def evaluate(
        self,
        model,
        distributed=False,
        half=False,
        trt_file=None,
        decoder=None,
        test_size=None,
    ):
        """
        COCO average precision (AP) Evaluation. Iterate inference on the test dataset
        and the results are evaluated by COCO API.

        NOTE: This function will change training mode to False, please save states if needed.

        Args:
            model : model to evaluate.

        Returns:
            ap50_95 (float) : COCO AP of IoU=50:95
            ap50 (float) : COCO AP of IoU=50
            summary (sr): summary info of evaluation.
        """
        # TODO half to amp_test
        if torch.cuda.is_available():
            tensor_type = torch.cuda.HalfTensor if half else torch.cuda.FloatTensor
        else:
            tensor_type = torch.float
        model = model.eval()
        if half:
            model = model.half()
        ids = []
        data_list = []
        progress_bar = tqdm if is_main_process() else iter

        inference_time = 0
        nms_time = 0
        num_full_batch_steps = len(self.dataloader) - 1
        n_samples = max(num_full_batch_steps - self.warmup_steps, 1)

        if trt_file is not None: # ignore this on cpu or hpu
            from torch2trt import TRTModule

            model_trt = TRTModule()
            model_trt.load_state_dict(torch.load(trt_file))

            x = torch.ones(1, 3, test_size[0], test_size[1]).cuda()
            model(x)
            model = model_trt

        first_start = time.time()
        for cur_iter, (imgs, _, info_imgs, ids) in enumerate(
            progress_bar(self.dataloader)
        ):
            with torch.no_grad():
                if self.use_hpu:
                    imgs = imgs.to(dtype=tensor_type, device=torch.device("hpu"))
                else:
                    imgs = imgs.type(tensor_type)

                # skip the the last iters since batchsize might be not enough for batch inference
                is_time_record = self.warmup_steps <= cur_iter < num_full_batch_steps
                if is_time_record:
                    start = time.time()

                outputs = model(imgs)
                if decoder is not None:
                    outputs = decoder(outputs, dtype=outputs.type())

                if self.cpu_post_processing:
                    htcore.mark_step()
                    outputs = outputs.to('cpu')

                if is_time_record:
                    infer_end = time_synchronized()
                    inference_time += infer_end - start

                outputs = self.postprocessor(outputs)

                if is_time_record:
                    nms_end = time_synchronized()
                    nms_time += nms_end - infer_end

            data_list.extend(self.convert_to_coco_format(outputs, info_imgs, ids))

        last_finish = time_synchronized()

        if torch.cuda.is_available():
            statistics = torch.cuda.FloatTensor([inference_time, nms_time, n_samples])
            first_start = torch.cuda.DoubleTensor([first_start])
            last_finish = torch.cuda.DoubleTensor([last_finish])

        else:
            statistics = torch.FloatTensor([inference_time, nms_time, n_samples])
            first_start = torch.DoubleTensor([first_start], device='cpu')
            last_finish = torch.DoubleTensor([last_finish], device='cpu')

        if distributed:
            data_list = gather(data_list, dst=0)
            data_list = list(itertools.chain(*data_list))
            torch.distributed.reduce(statistics, dst=0,
                                        group=torch.distributed.new_group(backend="gloo"))
            torch.distributed.reduce(first_start, dst=0, op=torch.distributed.ReduceOp.MIN,
                                        group=torch.distributed.new_group(backend="gloo"))
            torch.distributed.reduce(last_finish, dst=0, op=torch.distributed.ReduceOp.MAX,
                                        group=torch.distributed.new_group(backend="gloo"))

        statistics = torch.cat((statistics, last_finish - first_start), dim=-1)

        eval_results = self.evaluate_prediction(data_list, statistics)

        synchronize()
        return eval_results

    def convert_to_coco_format(self, outputs, info_imgs, ids):
        data_list = []
        for (output, img_h, img_w, img_id) in zip(
            outputs, info_imgs[0], info_imgs[1], ids
        ):
            if output.size(0) == 0:
                continue
            output = output.cpu()
            bboxes = output[:, 0:4]

            # preprocessing: resize
            scale = min(
                self.img_size[0] / float(img_h), self.img_size[1] / float(img_w)
            )
            bboxes /= scale
            bboxes = xyxy2xywh(bboxes)

            cls = output[:, 5]
            scores = (output[:, 4]).float()
            for ind in range(bboxes.shape[0]):
                label = self.dataloader.dataset.class_ids[int(cls[ind])]
                pred_data = {
                    "image_id": int(img_id),
                    "category_id": label,
                    "bbox": bboxes[ind].numpy().tolist(),
                    "score": scores[ind].numpy().item(),
                    "segmentation": [],
                }  # COCO json format
                data_list.append(pred_data)
        return data_list

    def evaluate_prediction(self, data_dict, statistics):
        if not is_main_process():
            return 0, 0, None

        logger.info("Evaluate in main process...")

        annType = ["segm", "bbox", "keypoints"]

        inference_time = statistics[0].item()
        nms_time = statistics[1].item()
        n_samples = statistics[2].item()
        total_time = statistics[3].item()
        # total_samles and total_samples_recorded can be different
        # due to warmup_steps and not counted last iteration
        total_samles = len(self.dataloader)
        total_samples_recorded = n_samples * self.dataloader.batch_size
        total_throughput = total_samles / total_time

        time_info = f"Total evaluation loop time: {total_time:.2f} (s)" + \
                    f"\nTotal throughput: {total_throughput:.2f} (images/s)"
        if inference_time and nms_time:
            a_infer_time = 1000 * inference_time / total_samples_recorded
            a_nms_time = 1000 * nms_time / total_samples_recorded
            a_infer_tp = total_samples_recorded / inference_time
            a_nms_tp = total_samples_recorded / nms_time
            a_total_tp = total_samples_recorded / (inference_time + nms_time)

            time_info += '\n' + "\n".join(
                [
                    "Average {} latency: {:.4f} (ms)".format(k, v)
                    for k, v in zip(
                        ["inference", "NMS", "(inference + NMS)"],
                        [a_infer_time, a_nms_time, (a_infer_time + a_nms_time)],
                    )
                ]
            )
            time_info += '\n' + "\n".join(
                [
                    "Average {} throughput: {:.4f} (images/s)".format(k, v)
                    for k, v in zip(
                        ["inference", "NMS", "(inference + NMS)"],
                        [a_infer_tp, a_nms_tp, a_total_tp],
                    )
                ]
            )

        info = time_info + "\n"

        # Evaluate the Dt (detection) json comparing with the ground truth
        if len(data_dict) > 0:
            cocoGt = self.dataloader.dataset.coco
            # TODO: since pycocotools can't process dict in py36, write data to json file.
            if self.testdev:
                json.dump(data_dict, open("./yolox_testdev_2017.json", "w"))
                cocoDt = cocoGt.loadRes("./yolox_testdev_2017.json")
            else:
                _, tmp = tempfile.mkstemp()
                json.dump(data_dict, open(tmp, "w"))
                cocoDt = cocoGt.loadRes(tmp)
            try:
                from yolox.layers import COCOeval_opt as COCOeval
            except ImportError:
                from pycocotools.cocoeval import COCOeval

                logger.warning("Use standard COCOeval.")

            cocoEval = COCOeval(cocoGt, cocoDt, annType[1])
            cocoEval.evaluate()
            cocoEval.accumulate()
            redirect_string = io.StringIO()
            with contextlib.redirect_stdout(redirect_string):
                cocoEval.summarize()
            info += redirect_string.getvalue()
            cat_ids = list(cocoGt.cats.keys())
            cat_names = [cocoGt.cats[catId]['name'] for catId in sorted(cat_ids)]
            if self.per_class_AP:
                AP_table = per_class_AP_table(cocoEval, class_names=cat_names)
                info += "per class AP:\n" + AP_table + "\n"
            if self.per_class_AR:
                AR_table = per_class_AR_table(cocoEval, class_names=cat_names)
                info += "per class AR:\n" + AR_table + "\n"
            return cocoEval.stats[0], cocoEval.stats[1], info
        else:
            return 0, 0, info
