"""
Bir klasordeki tum fis gorsellerini (sentetik + gercek) MinIO'ya toplu yukler.

Kullanim:
  1. Asagidaki KLASOR_YOLU, BUCKET_NAME, MINIO_* degerlerini kendine gore duzenle.
  2. pip install minio
  3. python toplu_yukle.py
"""
import os
from minio import Minio

# ---- Duzenlemen gereken degerler ----
# Her klasoru ("gercek", "sentetik" gibi) MinIO'da ayni isimli bir alt klasor (prefix) olarak saklariz.
KAYNAK_KLASORLER = {
    "gercek": r"C:\Users\cguya\Desktop\fisler\gercek",
    "sentetik": r"C:\Users\cguya\Desktop\fisler\sentetik",
}
BUCKET_GORSELLER = "masraf-fisler"      # jpg/png/pdf gibi ham dosyalar buraya
BUCKET_ETIKETLER = "masraf-etiketler"   # json (ground truth) dosyalari buraya

MINIO_ENDPOINT = "localhost:9000"
MINIO_ACCESS_KEY = "buraya_access_key"
MINIO_SECRET_KEY = "buraya_secret_key"
MINIO_SECURE = False
# --------------------------------------

GORSEL_UZANTILARI = (".png", ".jpg", ".jpeg", ".pdf", ".webp")
ETIKET_UZANTILARI = (".json",)

def klasoru_yukle(client, klasor_yolu, prefix, bucket_name, uzantilar):
    """Bir klasordeki tum uygun dosyalari MinIO'ya, verilen bucket ve prefix altinda yukler."""
    if not os.path.isdir(klasor_yolu):
        print(f"HATA: Klasor bulunamadi, atlaniyor: {klasor_yolu}")
        return 0, 0

    dosyalar = [
        f for f in os.listdir(klasor_yolu)
        if f.lower().endswith(uzantilar)
    ]

    if not dosyalar:
        print(f"'{prefix}' -> '{bucket_name}' icin yuklenecek uygun dosya bulunamadi.")
        return 0, 0

    print(f"\n'{prefix}' -> '{bucket_name}': {len(dosyalar)} dosya bulundu, yukleme basliyor...")

    basarili = 0
    basarisiz = 0

    for i, dosya_adi in enumerate(dosyalar, start=1):
        dosya_yolu = os.path.join(klasor_yolu, dosya_adi)
        object_name = f"{prefix}/{dosya_adi}"
        try:
            client.fput_object(bucket_name, object_name, dosya_yolu)
            print(f"[{i}/{len(dosyalar)}] OK  - {bucket_name}/{object_name}")
            basarili += 1
        except Exception as e:
            print(f"[{i}/{len(dosyalar)}] HATA - {object_name}: {e}")
            basarisiz += 1

    return basarili, basarisiz


def main():
    client = Minio(
        MINIO_ENDPOINT,
        access_key=MINIO_ACCESS_KEY,
        secret_key=MINIO_SECRET_KEY,
        secure=MINIO_SECURE,
    )

    for bucket_name in (BUCKET_GORSELLER, BUCKET_ETIKETLER):
        if not client.bucket_exists(bucket_name):
            client.make_bucket(bucket_name)
            print(f"Bucket olusturuldu: {bucket_name}")

    toplam_basarili = 0
    toplam_basarisiz = 0

    for prefix, klasor_yolu in KAYNAK_KLASORLER.items():
        # Ayni klasorden once gorselleri, sonra JSON etiketlerini ayikliyoruz
        b1, f1 = klasoru_yukle(client, klasor_yolu, prefix, BUCKET_GORSELLER, GORSEL_UZANTILARI)
        b2, f2 = klasoru_yukle(client, klasor_yolu, prefix, BUCKET_ETIKETLER, ETIKET_UZANTILARI)
        toplam_basarili += b1 + b2
        toplam_basarisiz += f1 + f2

    print(f"\n=== Tamamlandi. Toplam basarili: {toplam_basarili}, basarisiz: {toplam_basarisiz} ===")


if __name__ == "__main__":
    main()