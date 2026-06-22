# FastAPI Backend + ML Model
```main.py``` - FastAPI Traffic Classifier

## Overview
The backend service provides ML-based network traffic classification using the mm_cesnet_v1 model. It receives raw packet sequences and flow statistics from the Ryu SDN controller, classifies traffic into one of eight campus network categories, and returns QoS priority information.

## Request Format
Endpoint: POST /classify
Content-Type: application/json
The request expects:

- PPI sequences (3 lists of 30 values): packet sizes, inter-packet times (microseconds), and directions (1 = forward, -1 = backward)

- Flowstats (17 values): byte/packet counts, duration, roundtrips, and TCP flag counters

- All values must be integers, with exactly 30 values for each PPI sequence and exactly 17 for flowstats.

## Response Format
Status Code: 200 OK

The response returns:

- category — Campus traffic class (voip, cloud_email, dns, http, video, ftp, background, p2p)

- category_id — Numeric category index (0-7)

- confidence — Confidence score (0-1)

- priority — QoS priority (1-10, higher = more important)

- top_prediction — Best CESNET class with probability

- top5_predictions — Top 5 CESNET class predictions

- timestamp — Unix timestamp of request

## Campus Categories & Priority
The lower the value, the lower the priority
<pre>
| Category    | Priority |
|-------------|----------|
| voip        |   10     |
| cloud_email |   9      |
| dns         |   8      |
| http        |   5      |
| video       |   2      |
| ftp         |   2      |
| background  |   1      |
| p2p         |   1      |
</pre>

The model maps 191 CESNET-TLS22 classes to these 8 campus categories through keyword matching.

## Model Details
Architecture: mm_cesnet_v1 with CESNET_TLS22_WEEK40 weights

Inputs: PPI tensor (1, 3, 30) + FlowStats tensor (1, 17)

Outputs: 191 class logits

PPI Normalization: Standard scaling applied to packet sizes (mean 708.39, scale 581.24) and inter-packet times (mean 228.11, scale 1517.16), clipped to training ranges

## Usage Example
```bash
curl -X POST http://localhost:8000/classify \
  -H "Content-Type: application/json" \
  -d '{
    "sizes": [64]*30,
    "ipts": [0, 1200, 800, 3400] + [0]*26,
    "dirs": [1]*30,
    "flowstats": [1500, 500, 10, 5, 0.5, 30, 3, 0.45, 0, 0, 0, 0, 0, 0, 0, 0, 0]
  }'
```

## Dependencies
- fastapi, uvicorn — Web framework and server

- pydantic — Data validation

- torch, numpy — Deep learning and numerical operations

- cesnet-models — Pre-trained model

## Running the Service
```bash
source ~/ml-env/bin/activate
cd ~/SynthNet/backend
python main.py
```
The server starts on http://0.0.0.0:8000.

### Integration with Ryu Controller
The Ryu controller captures the first 30 packets of each flow, extracts PPI features and flowstats, sends a POST request to /classify, and uses the returned category and priority to install OpenFlow QoS rules.


