import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel

from app import config
from app.services import pipeline, vendors

router = APIRouter()
log = logging.getLogger(__name__)

# Satici sozlugu ilk istekte yuklenir. Dosya yoksa bos kalir, servis sozluksuz de calisir.
sozluk_deposu = vendors.SozlukDeposu()


class VknIstegi(BaseModel):
    fis_id: str
    ocr_text: str
    vlm_vkn: Optional[str] = None      # VLM bir VKN uretmisse (opsiyonel)
    firma_ipucu: Optional[str] = None  # VLM/OCR'in okudugu firma adi (opsiyonel)


def _mlflow_logla(fis_id: str, sonuc: dict) -> None:
    """Arka planda calisir: MLflow'a ulasilamazsa yalnizca log'a yazar, istegi etkilemez."""
    try:
        from app.services import tracking
        tracking.log_vkn_run(fis_id, sonuc)
    except Exception:
        log.warning("MLflow'a VKN run'i yazilamadi: %s", fis_id, exc_info=True)


@router.post("/vkn")
def vkn_cikar(payload: VknIstegi, arka_plan: BackgroundTasks):
    """
    OCR metninden VKN'yi hibrit akisla cikarir ve satici sozlugunden firmayi bulur.
    Sonuc 'insan_kontrolu: true' ise fis elle kontrole yonlendirilmelidir.
    MLflow logu yanit verildikten sonra arka planda yazilir ('mlflow_log' alani durumu soyler).
    """
    sonuc = pipeline.vkn_karar_ver(
        payload.ocr_text, payload.vlm_vkn, sozluk=sozluk_deposu.al(), firma_ipucu=payload.firma_ipucu
    )
    sonuc["fis_id"] = payload.fis_id

    if config.mlflow_log_aktif():
        arka_plan.add_task(_mlflow_logla, payload.fis_id, dict(sonuc))
        sonuc["mlflow_log"] = "kuyruga_alindi"
    else:
        sonuc["mlflow_log"] = "kapali"
    return sonuc


@router.get("/sozluk-durum")
def sozluk_durum():
    """Satici sozlugunun yuklenip yuklenmedigini gosterir."""
    return {"yuklu_satici_sayisi": len(sozluk_deposu.al()), "dosya": sozluk_deposu.yol}


@router.post("/sozluk-yenile")
def sozluk_yenile():
    """Sozluk dosyasini servisi yeniden baslatmadan tekrar yukler."""
    return {"yuklu_satici_sayisi": len(sozluk_deposu.yenile())}
