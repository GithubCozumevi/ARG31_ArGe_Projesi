"""
MinIO ile konusan servis katmani.
Butun bucket/dosya islemleri buradan gecer, boylece router'lar
MinIO'nun detaylarini bilmek zorunda kalmaz.
"""
import os
from datetime import timedelta
from functools import lru_cache
from typing import Optional

from minio import Minio
from minio.error import S3Error


@lru_cache(maxsize=1)
def get_client() -> Minio:
    """MinIO istemcisi ilk kullanimda kurulur (modul acilisinda degil); ayarlar o an .env'den okunur."""
    return Minio(
        os.getenv("MINIO_ENDPOINT", "localhost:9000"),
        access_key=os.getenv("MINIO_ACCESS_KEY"),
        secret_key=os.getenv("MINIO_SECRET_KEY"),
        secure=os.getenv("MINIO_SECURE", "false").lower() == "true",
    )


def ensure_bucket(bucket_name: str) -> None:
    """Bucket yoksa olusturur, varsa dokunmaz."""
    client = get_client()
    if not client.bucket_exists(bucket_name):
        client.make_bucket(bucket_name)


def upload_file(bucket_name: str, object_name: str, file_path: str) -> str:
    """Bir dosyayi MinIO'ya yukler, yuklenen dosyanin adini dondurur."""
    ensure_bucket(bucket_name)
    get_client().fput_object(bucket_name, object_name, file_path)
    return object_name


def download_file(bucket_name: str, object_name: str, destination_path: str) -> str:
    """MinIO'dan bir dosyayi indirir."""
    get_client().fget_object(bucket_name, object_name, destination_path)
    return destination_path


def object_exists(bucket_name: str, object_name: str) -> bool:
    """Bucket icinde bu adla bir dosya var mi?"""
    try:
        get_client().stat_object(bucket_name, object_name)
        return True
    except S3Error as e:
        if e.code in ("NoSuchKey", "NoSuchObject", "NoSuchBucket", "NotFound"):
            return False
        raise


def list_files(bucket_name: str, prefix: Optional[str] = None) -> list[str]:
    """Bir bucket icindeki tum dosyalarin adlarini listeler (alt klasorler dahil, ornek: 'gercek/fis1.jpg')."""
    client = get_client()
    if not client.bucket_exists(bucket_name):
        return []
    return [obj.object_name for obj in client.list_objects(bucket_name, prefix=prefix, recursive=True)]


def get_presigned_url(bucket_name: str, object_name: str, expires_seconds: int = 3600) -> str:
    """Gecici, paylasilabilir bir indirme linki uretir (varsayilan: 1 saat gecerli)."""
    return get_client().presigned_get_object(
        bucket_name, object_name, expires=timedelta(seconds=expires_seconds)
    )
