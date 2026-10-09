import logging
import os
import shutil
import tempfile
from typing import Optional

from fastapi import APIRouter, File, HTTPException, UploadFile

from app import config
from app.services import storage as storage_service

router = APIRouter()
log = logging.getLogger(__name__)

IZINLI_UZANTILAR = {".png", ".jpg", ".jpeg", ".pdf", ".webp"}


def _guvenli_ad(dosya_adi: Optional[str]) -> str:
    """Istemcinin verdigi dosya adindan yol parcalarini atar ('../x.jpg' -> 'x.jpg') ve uzantiyi denetler."""
    ad = os.path.basename((dosya_adi or "").replace("\\", "/"))
    if not ad or ad in (".", ".."):
        raise HTTPException(status_code=400, detail="Gecersiz dosya adi")
    if os.path.splitext(ad)[1].lower() not in IZINLI_UZANTILAR:
        raise HTTPException(
            status_code=400, detail=f"Desteklenmeyen dosya turu. Izinli: {', '.join(sorted(IZINLI_UZANTILAR))}"
        )
    return ad


@router.post("/upload")
def upload_receipt(file: UploadFile = File(...), overwrite: bool = False):
    """Bir fis/fatura gorselini MinIO'ya yukler. Ayni adli dosya varsa, overwrite=true verilmedikce 409 doner."""
    ad = _guvenli_ad(file.filename)
    bucket = config.minio_bucket()
    tmp_path = None
    try:
        if not overwrite and storage_service.object_exists(bucket, ad):
            raise HTTPException(status_code=409, detail="Bu adla bir dosya zaten var (ustune yazmak icin overwrite=true)")
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(ad)[1]) as tmp:
            shutil.copyfileobj(file.file, tmp)
            tmp_path = tmp.name
        storage_service.upload_file(bucket, ad, tmp_path)
    except HTTPException:
        raise
    except Exception:
        log.exception("MinIO'ya yukleme basarisiz: %s", ad)
        raise HTTPException(status_code=502, detail="Dosya depoya yazilamadi")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)

    return {"filename": ad, "bucket": bucket, "status": "uploaded"}


@router.get("/list")
def list_receipts(prefix: Optional[str] = None):
    """Bucket icindeki fis dosyalarini listeler (alt klasorler dahil). prefix='gercek/' gibi daraltilabilir."""
    bucket = config.minio_bucket()
    try:
        files = storage_service.list_files(bucket, prefix)
    except Exception:
        log.exception("MinIO listeleme basarisiz")
        raise HTTPException(status_code=502, detail="Depo listelenemedi")
    return {"bucket": bucket, "files": files}


@router.get("/url/{object_name:path}")
def get_download_url(object_name: str):
    """Bir dosya icin gecici indirme linki uretir. Alt klasorlu adlar desteklenir (gercek/fis1.jpg)."""
    bucket = config.minio_bucket()
    try:
        if not storage_service.object_exists(bucket, object_name):
            raise HTTPException(status_code=404, detail="Dosya bulunamadi")
        url = storage_service.get_presigned_url(bucket, object_name)
    except HTTPException:
        raise
    except Exception:
        log.exception("MinIO link uretimi basarisiz: %s", object_name)
        raise HTTPException(status_code=502, detail="Indirme linki uretilemedi")
    return {"object_name": object_name, "url": url}
