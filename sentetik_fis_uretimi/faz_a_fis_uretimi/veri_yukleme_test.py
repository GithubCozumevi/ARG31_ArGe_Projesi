"""
Basit test scripti: field_generator.py içindeki tüm *_urunleri_yukle
fonksiyonlarını tek tek çağırıp, dosya yollarının doğru olup olmadığını
ve kaç ürün/kalem yüklendiğini terminalde gösterir.

Kullanım:
    python -m faz_a_fis_uretimi.veri_yukleme_test

NOT: Bu scripti projenin kök dizininde (field_generator.py'nin import
edilebildiği yerde) çalıştırın, ya da PYTHONPATH'i ona göre ayarlayın.
"""

from faz_a_fis_uretimi.generators import field_generator as fg


def satir_yazdir(baslik: str, dosya_yolu, sonuc) -> None:
    dosya_var_mi = dosya_yolu.exists()
    durum = "✓ VAR" if dosya_var_mi else "✗ YOK"

    print(f"\n--- {baslik} ---")
    print(f"  Dosya yolu : {dosya_yolu}")
    print(f"  Durum      : {durum}")

    if not dosya_var_mi:
        print("  (dosya bulunamadi, fonksiyon boş sonuç dönmüş olmalı)")
        return

    if isinstance(sonuc, dict):
        if not sonuc:
            print("  Sonuç      : BOŞ dict döndü (0 kategori)")
            return
        toplam = sum(len(v) for v in sonuc.values())
        print(f"  Toplam ürün: {toplam}  ({len(sonuc)} kategoriye dağılmış)")
        for kategori, urunler in sonuc.items():
            kategori_adi = getattr(kategori, "value", kategori)
            print(f"    - {kategori_adi:<20}: {len(urunler)} ürün")
    elif isinstance(sonuc, list):
        print(f"  Toplam ürün: {len(sonuc)}")
    else:
        print(f"  Beklenmeyen dönüş tipi: {type(sonuc)}")


def main():
    print("=" * 60)
    print("  VERİ YÜKLEME TEST SCRİPTİ")
    print("=" * 60)

    testler = [
        ("market_urunleri_yukle (Market CSV)", fg.MARKET_URUNLERI_CSV, fg.market_urunleri_yukle),
        ("temiz_urunleri_yukle (Trendyol/Temiz Ürünler CSV)", fg.TEMIZ_URUNLER_CSV, fg.temiz_urunleri_yukle),
        ("yemek_urunleri_yukle (Restoran CSV)", fg.YEMEK_URUNLERI_CSV, fg.yemek_urunleri_yukle),
        ("danismanlik_urunleri_yukle", fg.DANISMANLIK_URUNLERI_CSV, fg.danismanlik_urunleri_yukle),
        ("konaklama_urunleri_yukle", fg.KONAKLAMA_URUNLERI_CSV, fg.konaklama_urunleri_yukle),
        ("ulasim_urunleri_yukle", fg.ULASIM_URUNLERI_CSV, fg.ulasim_urunleri_yukle),
        ("anomali_urunleri_yukle", fg.ANOMALI_URUNLERI_CSV, fg.anomali_urunleri_yukle),
    ]

    for baslik, dosya_yolu, fonksiyon in testler:
        try:
            sonuc = fonksiyon()
        except Exception as e:
            print(f"\n--- {baslik} ---")
            print(f"  Dosya yolu : {dosya_yolu}")
            print(f"  ✗ HATA: {e}")
            continue
        satir_yazdir(baslik, dosya_yolu, sonuc)

    print("\n" + "=" * 60)
    print("  TEST TAMAMLANDI")
    print("=" * 60)


if __name__ == "__main__":
    main()