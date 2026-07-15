#!/usr/bin/env python3
"""
SynthNet ML Backend — XGBoost classifier
Features: 10 selected from ISCX 30s NonVPN dataset via SelectKBest
"""

import logging
import numpy as np
import joblib
import uvicorn

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Optional
from contextlib import asynccontextmanager

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ── Selected features in EXACT order from SelectKBest ────────────────────────
SELECTED_FEATURES = [
    'duration',
    'total_fiat',
    'total_biat',
    'min_fiat',
    'min_biat',
    'max_fiat',
    'max_biat',
    'mean_biat',
    'max_flowiat',
    'mean_flowiat',
]

# ── QoS priority mapping ──────────────────────────────────────────────────────
PRIORITY_MAP = {
    'VOIP':      'high',
    'STREAMING': 'high',
    'FT':        'medium',
    'MAIL':      'medium',
    'BROWSING':  'low',
    'CHAT':      'low',
    'P2P':       'low',
}

# ── Model state ───────────────────────────────────────────────────────────────
model    = None
selector = None
encoder  = None


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global model, selector, encoder
    logger.info("Loading model artifacts...")
    model    = joblib.load("traffic_classifier.pkl")
    selector = joblib.load("feature_selector.pkl")
    encoder  = joblib.load("label_encoder.pkl")
    logger.info(f"Model loaded. Classes: {list(encoder.classes_)}")
    logger.info(f"Selected features: {SELECTED_FEATURES}")
    yield


app = FastAPI(
    title="SynthNet ML Backend",
    version="3.0.0",
    lifespan=lifespan,
)


# ── Request models ────────────────────────────────────────────────────────────

class FlowFeatures(BaseModel):
    """
    All flow features from FlowBuffer.get_flowstats().
    Only the 10 selected ones are used for inference —
    the rest are accepted but ignored (extra='allow').
    """
    duration:           float = 0.0
    total_fiat:         float = 0.0
    total_biat:         float = 0.0
    min_fiat:           float = 0.0
    min_biat:           float = 0.0
    max_fiat:           float = 0.0
    max_biat:           float = 0.0
    mean_fiat:          float = 0.0
    mean_biat:          float = 0.0
    max_flowiat:        float = 0.0
    mean_flowiat:       float = 0.0
    # Extra features accepted but not used
    min_flowiat:        float = 0.0
    std_flowiat:        float = 0.0
    flowPktsPerSecond:  float = 0.0
    flowBytesPerSecond: float = 0.0
    min_active:         float = 0.0
    mean_active:        float = 0.0
    max_active:         float = 0.0
    std_active:         float = 0.0
    min_idle:           float = 0.0
    mean_idle:          float = 0.0
    max_idle:           float = 0.0
    std_idle:           float = 0.0

    class Config:
        extra = 'allow'  # ignore any unknown fields from Ryu


class ClassifyResponse(BaseModel):
    label:      str
    confidence: float
    priority:   str
    all_probs:  dict


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/classify", response_model=ClassifyResponse)
def classify(flow: FlowFeatures):
    """
    Main endpoint — called by Ryu controller.
    Accepts flat flow feature dict, extracts the 10 selected features,
    runs XGBoost inference, returns label + confidence + priority.
    """
    try:
        # Build feature vector in exact SelectKBest order
        X = np.array([[
            getattr(flow, f, 0.0) for f in SELECTED_FEATURES
        ]], dtype=np.float64)

        # Replace any inf/nan
        X = np.nan_to_num(X, nan=0.0, posinf=0.0, neginf=0.0)

        # Predict — selector already applied during training,
        # X is already in the 10-feature space
        pred_encoded = model.predict(X)[0]
        proba        = model.predict_proba(X)[0]
        confidence   = float(proba.max())
        label        = encoder.inverse_transform([pred_encoded])[0]
        priority     = PRIORITY_MAP.get(label, 'low')

        all_probs = {
            cls: float(p)
            for cls, p in zip(encoder.classes_, proba)
        }

        logger.info(
            f"Classified: {label} "
            f"(confidence={confidence:.3f}, priority={priority})"
        )

        return ClassifyResponse(
            label=label,
            confidence=confidence,
            priority=priority,
            all_probs=all_probs,
        )

    except Exception as e:
        logger.error(f"Classification error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/health")
def health():
    return {
        "status":          "healthy",
        "model_loaded":    model is not None,
        "selector_loaded": selector is not None,
        "num_features":    len(SELECTED_FEATURES),
        "num_classes":     len(encoder.classes_) if encoder else 0,
        "classes":         list(encoder.classes_) if encoder else [],
        "selected_features": SELECTED_FEATURES,
    }


@app.get("/stats")
def stats():
    return {
        "features": SELECTED_FEATURES,
        "classes":  list(encoder.classes_) if encoder else [],
        "priority_map": PRIORITY_MAP,
    }


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        workers=1,
        log_level="info",
    )
