# Perception Interview Knowledge Map

Use this as a checklist when the interview involves computer vision, autonomous driving perception, parking, BEV, model deployment, or edge AI. Do not dump all topics into the recap; select only topics connected to the interview.

## BEV, IPM, LSS, FastBEV

- Explain the difference between IPM/TOP View and feature-level BEV.
- Mention IPM's ground-plane and stable-extrinsic assumptions.
- For LSS, cover image feature extraction, depth distribution or frustum lifting, splatting into BEV, and downstream heads.
- For FastBEV, be ready to explain efficiency tradeoffs and multi-camera feature fusion.
- Common follow-ups: calibration dependency, BEV resolution error, occlusion, slope/speed-bump robustness, compute budget.

## Parking Perception

- Cover four-fisheye camera setup, surround-view geometry, parking-slot detection, lane/slot line detection, freespace, obstacle/object detection, and heightmap when relevant.
- Discuss engineering tradeoffs: pure vision vs ultrasonic fusion, lightweight IPM models vs heavier BEV models, precision vs platform compute.
- Common follow-ups: temporal stability, confidence filtering, geometric constraints, abnormal pose handling, data closed loop.

## Multi-Task Models

- Explain shared backbone plus task-specific heads.
- Discuss loss balancing, task conflict, task priority, label quality, and evaluation metrics per task.
- Common follow-ups: dynamic loss weights, hard negative mining, multitask degradation, deployment memory/runtime impact.

## Detection and Segmentation

- Be ready on YOLO-style detection basics: anchors/anchor-free, NMS, confidence, IoU, mAP, recall/precision.
- For segmentation, cover mask resolution, class imbalance, thin-line structures, post-processing, and downstream tolerance.
- For lane/slot lines, mention centerline/offset representations when grid quantization is a concern.

## Deployment: PyTorch, ONNX, Quantization, TDA4

- Explain PyTorch to ONNX export, opset compatibility, unsupported operators, graph simplification, and runtime validation.
- For quantization, distinguish PTQ and QAT; mention calibration data, activation distribution, accuracy drop, per-channel vs per-tensor where relevant.
- For edge deployment, discuss latency, memory, operator support, batch size, precision, and platform-specific toolchain constraints.
- Common follow-ups: ScatterND, MatMul, BatchNorm folding, NMS export, EfficientNet-Lite, TIDL or embedded runtime adaptation.

## Data and Evaluation

- Discuss dataset creation, labeling quality, train/val split, scenario coverage, corner cases, and data closed loop.
- Mention scenario-specific metrics, not only generic loss curves.
- Common follow-ups: collecting failure cases, long-tail parking scenes, night/rain/glare, camera dirt, calibration drift.

## C++ and Engineering

- For algorithm engineering roles, be ready to discuss C++ basics, OpenCV preprocessing, ROS/Apollo-style middleware, debugging, profiling, and code integration.
- If the user is weaker in C++, turn it into a preparation plan rather than overstating proficiency.
