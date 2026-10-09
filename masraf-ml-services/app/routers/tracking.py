import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import tracking as tracking_service

router = APIRouter()
log = logging.getLogger(__name__)


class OcrRunIstegi(BaseModel):
    """Bir OCR/VLM cikariminin ozeti. Gercek PaddleOCR/VLM entegrasyonunda alanlar cikarim sonucundan doldurulur."""
    fis_id: str
    processing_time_sec: float
    confidence_score: float
    result_json: dict
    model_name: str = "paddleocr_vlm_v1"


@router.post("/log-ocr-run")
def log_ocr_run(payload: OcrRunIstegi):
    """
    Bir fisin OCR/VLM cikarim sonucunu MLflow'a loglar.
    Gercek entegrasyonda bu fonksiyon, OCR/VLM calistiktan hemen sonra cagrilir.
    """
    try:
        run_id = tracking_service.log_ocr_run(
            experiment_name="ocr-inference-tracking",
            run_name=f"ocr-{payload.fis_id}",
            model_name=payload.model_name,
            processing_time_sec=payload.processing_time_sec,
            confidence_score=payload.confidence_score,
            result_json=payload.result_json,
        )
    except Exception:
        log.exception("MLflow'a OCR run yazilamadi: %s", payload.fis_id)
        raise HTTPException(status_code=502, detail="MLflow'a yazilamadi")
    return {"status": "logged", "run_id": run_id}
