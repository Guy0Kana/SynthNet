#!/usr/bin/env python3

"""
FastAPI Backend for ML-Based Traffic Classification
Uses mm_cesnet_v1 with CESNET-TLS22_WEEK40 weights
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict
import numpy as np
import torch
import torch.nn.functional as F
import time
import logging

from cesnet_models.models import mm_cesnet_v1, MM_CESNET_V1_Weights

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ============================================================================
# Configuration
# ============================================================================

NUM_CESNET_CLASSES = 191

CESNET_TO_CAMPUS = {
    'voip': ['voip', 'sip', 'rtp', 'voip_audio', 'voip_video', 'skype_audio',
             'zoom_audio', 'teams_audio', 'whatsapp_call', 'facetime_audio'],
    
    'cloud_email': ['cloud', 'email', 'imap', 'pop3', 'smtp', 'gmail', 'outlook',
                    'icloud', 'dropbox', 'google_drive', 'onedrive', 'sharepoint',
                    'office365', 'google_cloud', 'aws', 'azure', 'salesforce'],
    
    'dns': ['dns', 'mdns', 'dns_over_tls', 'dns_over_https'],
    
    'http': ['http', 'https', 'web', 'browsing', 'rest_api', 'http_web',
             'wordpress', 'github', 'stackoverflow'],
    
    'video': ['video', 'youtube', 'netflix', 'hulu', 'disney_plus', 'amazon_prime',
              'vimeo', 'twitch', 'tiktok', 'instagram_video', 'facebook_video'],
    
    'ftp': ['ftp', 'sftp', 'scp', 'rsync', 'file_transfer'],
    
    'background': ['background', 'idle', 'keepalive', 'ntp', 'dns_background',
                   'icmp', 'arp', 'ssh_background', 'snmp'],
    
    'p2p': ['p2p', 'bittorrent', 'torrent', 'peer_to_peer', 'utorrent',
            'libtorrent', 'bittorrent_dht', 'p2p_streaming'],
}

CAMPUS_CATEGORIES = ['voip', 'cloud_email', 'dns', 'http', 'video', 'ftp', 'background', 'p2p']

PRIORITY_MAP = {
    'voip': 10,
    'cloud_email': 9,
    'dns': 8,
    'http': 5,
    'video': 2,
    'ftp': 2,
    'background': 1,
    'p2p': 1,
}

# ============================================================================
# PPI Transform
# ============================================================================

class PPITransform:
    def __init__(self):
        self.psize_mean = 708.39
        self.psize_scale = 581.24
        self.ipt_mean = 228.11
        self.ipt_scale = 1517.16
    
    def __call__(self, tensor):
        t = tensor.clone()
        t[:, 0, :] = (t[:, 0, :] - self.psize_mean) / self.psize_scale
        t[:, 0, :] = torch.clamp(t[:, 0, :], min=-1.0, max=1.0)
        t[:, 1, :] = (t[:, 1, :] - self.ipt_mean) / self.ipt_scale
        t[:, 1, :] = torch.clamp(t[:, 1, :], min=-1.0, max=1.0)
        t[:, 2, :] = torch.clamp(t[:, 2, :], min=-1.0, max=1.0)
        return t

# ============================================================================
# Traffic Class Mapper
# ============================================================================

class TrafficClassMapper:
    def __init__(self):
        self.specific_to_campus = {}
        for campus_cat, specific_names in CESNET_TO_CAMPUS.items():
            for name in specific_names:
                self.specific_to_campus[name] = campus_cat
        self.default_category = 'background'

    def map(self, cesnet_class_name):
        name = cesnet_class_name.lower()
        if name in self.specific_to_campus:
            return self.specific_to_campus[name]
        for specific_name, campus_cat in self.specific_to_campus.items():
            if specific_name in name or name in specific_name:
                return campus_cat
        return self.default_category

# ============================================================================
# Model Loader
# ============================================================================

class TrafficClassifier:
    def __init__(self):
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        
        self.model = mm_cesnet_v1(
            weights=MM_CESNET_V1_Weights.CESNET_TLS22_WEEK40
        )
        self.ppi_transform = PPITransform()
        
        self.model.to(self.device)
        self.model.eval()
        
        self.mapper = TrafficClassMapper()
        self.total_predictions = 0
        self.stats = {cat: 0 for cat in CAMPUS_CATEGORIES}
        
        logger.info(f"Model loaded on {self.device}")
    
    def get_cesnet_class_name(self, class_id):
        try:
            from cesnet_models.datasets import CESNET_TLS22
            return CESNET_TLS22.classes[class_id]
        except (ImportError, AttributeError):
            return f"class_{class_id}"
    
    def predict(self, sizes, ipts, dirs, flowstats):
        # Build PPI tensor: (1, 3, 30)
        ppi_tensor = torch.tensor([
            sizes, ipts, dirs
        ], dtype=torch.float32).unsqueeze(0)
        
        ppi_tensor = self.ppi_transform(ppi_tensor)
        flowstats_tensor = torch.tensor([flowstats], dtype=torch.float32)
        
        with torch.no_grad():
            ppi_tensor = ppi_tensor.to(self.device)
            flowstats_tensor = flowstats_tensor.to(self.device)
            logits = self.model((ppi_tensor, flowstats_tensor))
            probabilities = F.softmax(logits, dim=1)
        
        probs = probabilities[0].cpu().numpy()
        
        # Convert all NumPy types to Python native types
        top5_ids = np.argsort(probs)[-5:][::-1]
        top5 = [{
            'class_id': int(i),
            'class_name': str(self.get_cesnet_class_name(i)),
            'probability': float(probs[i])
        } for i in top5_ids]
        
        best_id = int(top5_ids[0])
        best_class_name = str(self.get_cesnet_class_name(best_id))
        campus_category = str(self.mapper.map(best_class_name))
        confidence = float(probs[best_id])
        
        self.stats[campus_category] += 1
        self.total_predictions += 1
        
        return {
            'category': campus_category,
            'category_id': int(CAMPUS_CATEGORIES.index(campus_category)),
            'confidence': confidence,
            'priority': int(PRIORITY_MAP.get(campus_category, 4)),
            'top_prediction': {
                'class_id': best_id,
                'class_name': best_class_name,
                'probability': confidence
            },
            'top5_predictions': top5,
        }

# ============================================================================
# FastAPI Application
# ============================================================================

app = FastAPI(
    title="SynthNet Traffic Classifier",
    version="1.0.0"
)

classifier = TrafficClassifier()

# ============================================================================
# Request/Response Models
# ============================================================================

class RawFlowRequest(BaseModel):
    sizes: List[int]
    ipts: List[int]
    dirs: List[int]
    flowstats: List[float]
    
    class Config:
        json_schema_extra = {
            "example": {
                "sizes": [64] * 30,
                "ipts": [0, 1200, 800, 3400] + [0] * 26,
                "dirs": [1] * 30,
                "flowstats": [0.0] * 17
            }
        }


class PredictResponse(BaseModel):
    category: str
    category_id: int
    confidence: float
    priority: int
    top_prediction: Dict = None
    top5_predictions: List = None
    timestamp: float


class HealthResponse(BaseModel):
    status: str
    model_loaded: bool
    cesnet_classes: int
    campus_categories: List[str]

# ============================================================================
# API Endpoints
# ============================================================================

@app.get("/", response_model=HealthResponse)
async def root():
    return {
        "status": "running",
        "model_loaded": classifier.model is not None,
        "cesnet_classes": NUM_CESNET_CLASSES,
        "campus_categories": CAMPUS_CATEGORIES
    }


@app.get("/health", response_model=HealthResponse)
async def health_check():
    return {
        "status": "healthy",
        "model_loaded": classifier.model is not None,
        "cesnet_classes": NUM_CESNET_CLASSES,
        "campus_categories": CAMPUS_CATEGORIES
    }


@app.post("/classify", response_model=PredictResponse)
async def classify_flow(request: RawFlowRequest):
    start_time = time.time()
    
    if len(request.sizes) != 30 or len(request.ipts) != 30 or len(request.dirs) != 30:
        raise HTTPException(
            status_code=400,
            detail=f"PPI sequences must have exactly 30 values"
        )
    
    if len(request.flowstats) != 17:
        raise HTTPException(
            status_code=400,
            detail=f"flowstats must have exactly 17 values"
        )
    
    try:
        result = classifier.predict(
            request.sizes,
            request.ipts,
            request.dirs,
            request.flowstats
        )
        
        return PredictResponse(
            category=result['category'],
            category_id=result['category_id'],
            confidence=result['confidence'],
            priority=result['priority'],
            top_prediction=result.get('top_prediction'),
            top5_predictions=result.get('top5_predictions'),
            timestamp=start_time
        )
        
    except Exception as e:
        logger.error(f"Prediction error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats")
async def get_stats():
    return {
        'total_predictions': classifier.total_predictions,
        'campus_category_counts': classifier.stats,
        'device': str(classifier.device)
    }


@app.get("/mapping")
async def get_mapping():
    return {
        'cesnet_to_campus': CESNET_TO_CAMPUS,
        'campus_categories': CAMPUS_CATEGORIES,
        'priority_map': PRIORITY_MAP
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
        workers=4
    )
