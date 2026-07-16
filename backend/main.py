#!/usr/bin/env python3

"""
FastAPI Backend for ML-Based Traffic Classification
Uses XGBoost with 10 statistical features
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List
import numpy as np
import joblib
import os
import time
import logging
import warnings

warnings.filterwarnings('ignore', category=UserWarning, module='sklearn')

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# Configuration
# ============================================================================

# Paths to model files
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, 'model', 'traffic_classifier.pkl')
SELECTOR_PATH = os.path.join(BASE_DIR, 'model', 'feature_selector.pkl')

# Label mapping (hardcoded - matches training)
LABEL_MAP = {
    0: 'BROWSING',
    1: 'CHAT',
    2: 'FT',
    3: 'MAIL',
    4: 'P2P',
    5: 'STREAMING',
    6: 'VOIP'
}

# Convert to lowercase for Ryu compatibility
LABEL_MAP_LOWER = {k: v.lower() for k, v in LABEL_MAP.items()}

# QoS Priority Mapping (higher number = higher priority)
# NOTE: kept for reference / the /mapping endpoint. The Ryu controller
# keeps its own copy of this map (it's what actually sets OpenFlow
# priority) -- make sure the two stay in sync if you change either.
PRIORITY_MAP = {
    'voip': 10,
    'streaming': 7,
    'mail': 6,
    'browsing': 5,
    'chat': 4,
    'ft': 3,
    'p2p': 2,
    'default': 4,
}

# ============================================================================
# Request/Response Models
# ============================================================================

class ClassifyRequest(BaseModel):
    """Request model for XGBoost classification"""
    features: List[float]  # 10 features in exact order

    class Config:
        json_schema_extra = {
            "example": {
                "features": [
                    29999857.0,   # duration
                    29999857.0,   # total_fiat
                    29975545.0,   # total_biat
                    1.0,          # min_fiat
                    0.0,          # min_biat
                    1014690.0,    # max_fiat
                    1016593.0,    # max_biat
                    368.53,       # mean_biat
                    1014690.0,    # max_flowiat
                    1756.33       # mean_flowiat
                ]
            }
        }


class ClassifyResponse(BaseModel):
    """Response model for XGBoost classification"""
    traffic_type: str
    traffic_type_id: int
    confidence: float
    priority: int
    timestamp: float


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    selector_loaded: bool
    num_features: int
    num_classes: int
    classes: List[str]

# ============================================================================
# XGBoost Classifier
# ============================================================================

class XGBoostClassifier:
    def __init__(self):
        self.model = None
        self.selector = None
        self.num_features = 10
        self.num_classes = 7

        self.total_predictions = 0
        self.stats = {label: 0 for label in LABEL_MAP.values()}

        self._load_model()

    def _load_model(self):
        """Load XGBoost model and feature selector"""
        try:
            # Check if files exist
            if not os.path.exists(MODEL_PATH):
                raise FileNotFoundError(f"Model not found at: {MODEL_PATH}")
            if not os.path.exists(SELECTOR_PATH):
                raise FileNotFoundError(f"Selector not found at: {SELECTOR_PATH}")

            # Load model and selector
            self.model = joblib.load(MODEL_PATH)
            self.selector = joblib.load(SELECTOR_PATH)

            logger.info(f"✅ Model loaded from: {MODEL_PATH}")
            logger.info(f"✅ Selector loaded from: {SELECTOR_PATH}")
            logger.info(f"   Features: {self.num_features}")
            logger.info(f"   Classes: {self.num_classes}")

        except Exception as e:
            logger.error(f"❌ Failed to load model: {e}")
            raise

    def predict(self, features):
        """
        Predict traffic type from 10 features

        Args:
            features: List of 10 float values

        Returns:
            dict with traffic_type, confidence, etc.
        """
        # Convert to numpy array
        features_array = np.array(features).reshape(1, -1)

        # Validate feature count
        if features_array.shape[1] != self.num_features:
            raise ValueError(
                f"Expected {self.num_features} features, got {features_array.shape[1]}"
            )

        # Apply feature selection (selector transforms 10->10 if already selected)
        # If your selector expects 15 features, adjust accordingly
        try:
            features_selected = self.selector.transform(features_array)
        except Exception:
            # If selector expects different input, use features directly
            # (Your Ryu already sends the 10 selected features)
            features_selected = features_array

        # Get predictions
        pred_class = self.model.predict(features_selected)[0]
        probabilities = self.model.predict_proba(features_selected)[0]
        confidence = float(np.max(probabilities))

        # Map to label
        traffic_type = LABEL_MAP.get(pred_class, 'unknown')
        traffic_type_lower = traffic_type.lower()

        # Update stats
        self.total_predictions += 1
        self.stats[traffic_type] = self.stats.get(traffic_type, 0) + 1

        return {
            'traffic_type': traffic_type_lower,
            'traffic_type_id': int(pred_class),
            'confidence': confidence,
            'priority': PRIORITY_MAP.get(traffic_type_lower, 4),
            'traffic_type_original': traffic_type,
            'probabilities': probabilities.tolist()
        }

    def get_stats(self):
        return {
            'total_predictions': self.total_predictions,
            'traffic_type_counts': self.stats
        }

# ============================================================================
# FastAPI Application
# ============================================================================

app = FastAPI(
    title="SynthNet Traffic Classifier (XGBoost)",
    version="2.0.0",
    description="XGBoost-based traffic classification with 10 statistical features"
)

# Initialize classifier
try:
    classifier = XGBoostClassifier()
    logger.info("=" * 60)
    logger.info("🚀 SynthNet XGBoost Classifier Ready")
    logger.info("=" * 60)
except Exception as e:
    logger.error(f"Failed to initialize classifier: {e}")
    classifier = None

# ============================================================================
# API Endpoints
# ============================================================================

@app.get("/", response_model=HealthResponse)
async def root():
    if classifier is None:
        raise HTTPException(status_code=503, detail="Classifier not initialized")

    return {
        "status": "running",
        "model_loaded": classifier.model is not None,
        "selector_loaded": classifier.selector is not None,
        "num_features": classifier.num_features,
        "num_classes": classifier.num_classes,
        "classes": list(LABEL_MAP.values())
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    if classifier is None:
        raise HTTPException(status_code=503, detail="Classifier not initialized")

    return {
        "status": "healthy",
        "model_loaded": classifier.model is not None,
        "selector_loaded": classifier.selector is not None,
        "num_features": classifier.num_features,
        "num_classes": classifier.num_classes,
        "classes": list(LABEL_MAP.values())
    }


@app.post("/classify", response_model=ClassifyResponse)
async def classify_flow(request: ClassifyRequest):
    """
    Classify traffic using XGBoost model

    Expects 10 features in this exact order:
    1. duration
    2. total_fiat
    3. total_biat
    4. min_fiat
    5. min_biat
    6. max_fiat
    7. max_biat
    8. mean_biat
    9. max_flowiat
    10. mean_flowiat
    """
    start_time = time.time()

    if classifier is None:
        raise HTTPException(status_code=503, detail="Classifier not initialized")

    # Validate feature count
    if len(request.features) != classifier.num_features:
        raise HTTPException(
            status_code=400,
            detail=f"Expected {classifier.num_features} features, got {len(request.features)}"
        )

    try:
        result = classifier.predict(request.features)

        return ClassifyResponse(
            traffic_type=result['traffic_type'],
            traffic_type_id=result['traffic_type_id'],
            confidence=result['confidence'],
            priority=result['priority'],
            timestamp=start_time
        )

    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"Prediction error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats")
async def get_stats():
    if classifier is None:
        raise HTTPException(status_code=503, detail="Classifier not initialized")

    return classifier.get_stats()


@app.get("/mapping")
async def get_mapping():
    return {
        'label_map': LABEL_MAP,
        'priority_map': PRIORITY_MAP,
        'feature_order': [
            'duration',
            'total_fiat',
            'total_biat',
            'min_fiat',
            'min_biat',
            'max_fiat',
            'max_biat',
            'mean_biat',
            'max_flowiat',
            'mean_flowiat'
        ],
        'num_features': 10,
        'num_classes': 7
    }


@app.post("/test")
async def test_prediction():
    """Test endpoint with sample features"""
    if classifier is None:
        raise HTTPException(status_code=503, detail="Classifier not initialized")

    sample_features = [
        29999857.0,   # duration
        29999857.0,   # total_fiat
        29975545.0,   # total_biat
        1.0,          # min_fiat
        0.0,          # min_biat
        1014690.0,    # max_fiat
        1016593.0,    # max_biat
        368.53,       # mean_biat
        1014690.0,    # max_flowiat
        1756.33       # mean_flowiat
    ]

    result = classifier.predict(sample_features)
    return {
        "test_result": result,
        "message": "Test prediction successful"
    }


# ============================================================================
# Main Entry Point
# ============================================================================

if __name__ == "__main__":
    import uvicorn

    logger.info("=" * 60)
    logger.info("Starting SynthNet XGBoost API Server")
    logger.info("=" * 60)
    logger.info(f"Model: {MODEL_PATH}")
    logger.info(f"Selector: {SELECTOR_PATH}")
    logger.info("=" * 60)

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=False,
        log_level="info"
    )
