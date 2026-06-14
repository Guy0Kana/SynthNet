#!/usr/bin/env python3

"""
FastAPI Backend for ML-Based Traffic Classification
Uses pretrained cesnet-models 30pktTCNET (191 classes) mapped to campus traffic types
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import List, Dict
import numpy as np
import torch
import torch.nn.functional as F
import time
import logging

# Load pretrained model from cesnet-models
from cesnet_models.architectures.multimodal_cesnet import MultimodalCesnet

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


NUM_CESNET_CLASSES = 191

# Map CESNET-TLS22 classes to campus traffic categories
CESNET_TO_CAMPUS = {
    # VoIP (High priority)
    'voip': ['voip', 'sip', 'rtp', 'voip_audio', 'voip_video', 'skype_audio',
             'zoom_audio', 'teams_audio', 'whatsapp_call', 'facetime_audio'],
    
    # Cloud/Email (High priority)
    'cloud_email': ['cloud', 'email', 'imap', 'pop3', 'smtp', 'gmail', 'outlook',
                    'icloud', 'dropbox', 'google_drive', 'onedrive', 'sharepoint',
                    'office365', 'google_cloud', 'aws', 'azure', 'salesforce'],
    
    # HTTP/Web (Medium priority)
    'http': ['http', 'https', 'web', 'browsing', 'rest_api', 'http_web',
             'wordpress', 'github', 'stackoverflow'],
    
    # FTP/File Transfer (Low priority)
    'ftp': ['ftp', 'sftp', 'scp', 'rsync', 'file_transfer', 'dropbox_upload',
            'google_drive_upload', 'onedrive_upload'],
    
    # Background (Low priority)
    'background': ['background', 'idle', 'keepalive', 'ntp', 'dns_background',
                   'icmp', 'arp', 'ssh_background'],
    
    # Video streaming (Low priority)
    'video':  ['video', 'youtube', 'netflix', 'hulu', 'disney_plus', 'amazon_prime',
              'vimeo', 'twitch', 'tiktok', 'instagram_video', 'facebook_video'],
    
    # P2P (Low priority)
    'p2p': ['p2p', 'bittorrent', 'torrent', 'peer_to_peer', 'utorrent',
            'libtorrent', 'bittorrent_dht', 'p2p_streaming'],
    
    # DNS (Medium priority)
    'dns': ['dns', 'mdns', 'dns_over_tls', 'dns_over_https'],
}

# Campus traffic categories (8 classes for QoS)
CAMPUS_CATEGORIES = ['voip', 'cloud_email', 'http', 'ftp', 'background', 'video', 'p2p', 'dns']

# QoS priority mapping (higher number = higher priority)
PRIORITY_MAP = {
    'voip': 10,
    'cloud_email': 8,
    'dns': 7,
    'http': 5,
    'video': 3,
    'p2p': 2,
    'ftp': 3,
    'background': 2,
}

# Traffic Class Mapper

class TrafficClassMapper: 
    def __init__(self):
        self.specific_to_campus = {}
        for campus_cat, specific_names in CESNET_TO_CAMPUS.items():
            for name in specific_names:
                self.specific_to_campus[name] = campus_cat

        # Default fallback
        self.default_category = 'background'

    def map(self, cesnet_class_name):
        # Normalize to lowercase
        name = cesnet_class_name.lower()

        # Check exact match
        if name in self.specific_to_campus:
            return self.specific_to_campus[name]

        # Check partial match (e.g., 'google_drive_upload' contains 'google_drive')
        for specific_name, campus_cat in self.specific_to_campus.items():
            if specific_name in name or name in specific_name:
                return campus_cat

        # Fallback
        return self.default_category

    def get_campus_id(self, cesnet_class_name):
        """Get campus category ID (0-7) for a CESNET class name"""
        campus_cat = self.map(cesnet_class_name)
        return CAMPUS_CATEGORIES.index(campus_cat)
    
    def get_priority(self, cesnet_class_name):
        """Get QoS priority for a CESNET class name"""
        campus_cat = self.map(cesnet_class_name)
        return PRIORITY_MAP.get(campus_cat, 4)


# ============================================================================
# Model Loader
# ============================================================================

class TrafficClassifier:
    """Wrapper for pretrained 30pktTCNET model with 191-class to 8-class mapping"""
    
    def __init__(self, model_path=None, use_pretrained=True):
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        
        # Load pretrained model with 191 output classes (CESNET-TLS22)
        self.model = MultimodalCesnet(n_classes=NUM_CESNET_CLASSES)
        
        if use_pretrained:
            # Try cesnet-models built-in loading first
            try:
                from cesnet_models import load_model
                self.model = load_model('multimodal_cesnet', pretrained=True)
                logger.info("Loaded pretrained model via cesnet-models")
            except (ImportError, Exception) as e:
                logger.warning(f"Could not load via cesnet-models: {e}")
                
                # Fallback: load from checkpoint
                if model_path:
                    self.load_model(model_path)
                else:
                    logger.warning("No model path provided. Using random weights for testing.")
        elif model_path:
            self.load_model(model_path)
        else:
            logger.warning("Using random weights (for testing only)")
        
        self.model.to(self.device)
        self.model.eval()
        
        # Initialize mapper
        self.mapper = TrafficClassMapper()
        
        # Statistics
        self.total_predictions = 0
        self.stats = {cat: 0 for cat in CAMPUS_CATEGORIES}
        self.raw_class_stats = {}
        
        logger.info(f"Model loaded on {self.device}")
        logger.info(f"CESNET classes: {NUM_CESNET_CLASSES}")
        logger.info(f"Campus categories: {CAMPUS_CATEGORIES}")
    
    def load_model(self, model_path):
        """Load pretrained weights from checkpoint"""
        try:
            state_dict = torch.load(model_path, map_location=self.device)
            
            # Handle potential mismatches (e.g., different class counts)
            model_state = self.model.state_dict()
            filtered_state = {k: v for k, v in state_dict.items() 
                             if k in model_state and v.shape == model_state[k].shape}
            
            if len(filtered_state) != len(model_state):
                logger.warning(f"Loaded {len(filtered_state)}/{len(model_state)} layers")
            
            self.model.load_state_dict(filtered_state, strict=False)
            logger.info(f"Model loaded from {model_path}")
        except Exception as e:
            logger.error(f"Failed to load model: {e}")
    
    def get_cesnet_class_name(self, class_id):
        """
        Get CESNET class name from class ID.
        This would need a mapping file from cesnet-models.
        """
        # Placeholder - in production, load the actual class names
        # from cesnet-models or a mapping file
        class_names = {i: f"class_{i}" for i in range(NUM_CESNET_CLASSES)}
        return class_names.get(class_id, f"unknown_{class_id}")
    
    def predict(self, sizes, ipts, dirs):
        """
        Predict traffic category from raw sequences.
        
        Args:
            sizes: List of 30 packet sizes
            ipts: List of 30 inter-packet times (first is 0)
            dirs: List of 30 directions (1 or -1)
        
        Returns:
            dict with category, confidence, priority, and raw predictions
        """
        # Build tensor: (1, 3, 30) - channels first
        feature_tensor = torch.tensor([
            sizes,   # SIZE sequence (30 values)
            ipts,    # IPT sequence (30 values)
            dirs     # DIR sequence (30 values)
        ], dtype=torch.float32).unsqueeze(0)
        
        with torch.no_grad():
            feature_tensor = feature_tensor.to(self.device)
            logits = self.model(feature_tensor)
            probabilities = F.softmax(logits, dim=1)
        
        # Get top predictions
        probs = probabilities[0].cpu().numpy()
        
        # Get top 5 predictions for debugging
        top5_ids = np.argsort(probs)[-5:][::-1]
        top5 = [{'class_id': int(i), 'class_name': self.get_cesnet_class_name(i), 
                 'probability': float(probs[i])} for i in top5_ids]
        
        # Map to campus category using highest probability
        best_id = top5_ids[0]
        best_class_name = self.get_cesnet_class_name(best_id)
        campus_category = self.mapper.map(best_class_name)
        confidence = float(probs[best_id])
        
        # Calculate confidence as sum of probabilities for the mapped category
        # This aggregates all CESNET classes that map to the same campus category
        category_confidence = 0.0
        for class_id, prob in enumerate(probs):
            class_name = self.get_cesnet_class_name(class_id)
            if self.mapper.map(class_name) == campus_category:
                category_confidence += float(prob)
        category_confidence = min(category_confidence, 1.0)
        
        # Update statistics
        self.stats[campus_category] += 1
        self.total_predictions += 1
        
        # Track raw class distribution
        raw_class_name = best_class_name
        self.raw_class_stats[raw_class_name] = self.raw_class_stats.get(raw_class_name, 0) + 1
        
        return {
            'category': campus_category,
            'category_id': CAMPUS_CATEGORIES.index(campus_category),
            'category_confidence': category_confidence,
            'top_prediction': {
                'class_id': best_id,
                'class_name': best_class_name,
                'probability': confidence
            },
            'top5_predictions': top5,
            'confidence': confidence,  # Legacy field
            'priority': self.mapper.get_priority(best_class_name),
            'all_probabilities': {cat: 0.0 for cat in CAMPUS_CATEGORIES},  # Simplified
        }


# ============================================================================
# FastAPI Application
# ============================================================================

app = FastAPI(
    title="SynthNet Traffic Classifier",
    description="30pktTCNET-based traffic classification (191 classes → 8 campus categories)",
    version="1.0.0"
)

# Initialize model
classifier = TrafficClassifier(model_path=None, use_pretrained=True)

# ============================================================================
# Request/Response Models
# ============================================================================

class RawFlowRequest(BaseModel):
    """Request model for raw packet sequences"""
    sizes: List[int]  # Packet sizes (bytes), length = 30
    ipts: List[int]   # Inter-packet times (microseconds), length = 30
    dirs: List[int]   # Directions (1 = forward, -1 = backward), length = 30
    
    class Config:
        json_schema_extra = {
            "example": {
                "sizes": [64] * 30,
                "ipts": [0, 1200, 800, 3400] + [0] * 26,
                "dirs": [1] * 30
            }
        }


class PredictResponse(BaseModel):
    """Response model for prediction"""
    category: str
    category_id: int
    confidence: float
    priority: int
    category_confidence: float = None
    top_prediction: Dict = None
    top5_predictions: List = None
    all_probabilities: Dict[str, float] = None
    timestamp: float


class HealthResponse(BaseModel):
    """Health check response"""
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
    """
    Classify network traffic from raw packet sequences.
    
    Expects three lists of length 30:
    - sizes: Packet sizes in bytes
    - ipts: Inter-packet times in microseconds (first value is 0)
    - dirs: Direction (1 = forward, -1 = backward)
    
    Returns campus traffic category (voip, video, http, ftp, background, cloud, p2p, dns)
    with QoS priority.
    """
    start_time = time.time()
    
    # Validate input lengths
    if len(request.sizes) != 30 or len(request.ipts) != 30 or len(request.dirs) != 30:
        raise HTTPException(
            status_code=400,
            detail=f"Each sequence must have exactly 30 values. Got sizes:{len(request.sizes)}, ipts:{len(request.ipts)}, dirs:{len(request.dirs)}"
        )
    
    try:
        result = classifier.predict(request.sizes, request.ipts, request.dirs)
        
        return PredictResponse(
            category=result['category'],
            category_id=result['category_id'],
            confidence=result['confidence'],
            priority=result['priority'],
            category_confidence=result.get('category_confidence'),
            top_prediction=result.get('top_prediction'),
            top5_predictions=result.get('top5_predictions'),
            all_probabilities=result.get('all_probabilities'),
            timestamp=start_time
        )
        
    except Exception as e:
        logger.error(f"Prediction error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/stats")
async def get_stats():
    """Get model statistics"""
    return {
        'total_predictions': classifier.total_predictions,
        'campus_category_counts': classifier.stats,
        'raw_class_distribution': classifier.raw_class_stats,
        'device': str(classifier.device)
    }


@app.get("/mapping")
async def get_mapping():
    """Get CESNET to campus category mapping"""
    return {
        'cesnet_to_campus': CESNET_TO_CAMPUS,
        'campus_categories': CAMPUS_CATEGORIES,
        'priority_map': PRIORITY_MAP
    }


# ============================================================================
# Run the application
# ============================================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info"
    )
