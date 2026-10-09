"""
Üretilen veri setinin (main.py çıktısı) kalitesini özetleyen bağımsız
diagnostic script. main.py --anomali-orani ile üretim yaptıktan sonra
çalıştırılır, üretim koduna hiçbir bağımlılığı yok -- sadece çıktı
dosyalarını (etiketler.json + rapor.json) okur.

Kullanım:
    python -m faz_a_fis_uretimi.rapor_analiz --output-dir data --filename faturalar

Kontrol ettikleri:
  1. Hedeflenen anomali orani (--anomali-orani main.py'a girilen) ile
     gerçekte üretilen orani karşilaştirir (VKN/ad filtresinden SONRAKI
     hale göre -- kullaniciyi ilgilendiren, elindeki veri setinin gerçek
     orani).
  2. Her anomali türünün (anomali_turleri) kaç faturada geçtiğini VE
     yüzdesini (hem toplam faturaya hem anomalili faturaya göre) verir.
     NOT: union/additive etiketleme nedeniyle bir faturada birden fazla
     tür ayni anda olabilir -- yüzdeler toplamda %100'ü AŞABİLİR, bu
     normal (kaç faturada BİRDEN FAZLA tür var, o da ayrica raporlanir).
  3. VKN/ad tutarsizligi yüzünden elenen fatura sayisi + talep edilen
     adetle (--count) rekonsiliasyon (talep - elenen == toplam_fatura mi?).
  4. ANOMALI_FONKSIYONLARI listesindeki türlerden hiç üretilmemiş olanlari
     ayrica uyari olarak basar (validators.veri_seti_dogrula ile ayni
     mantik, ama bu script Fatura nesnesi kurmadan sadece JSON'dan okur).
"""

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import cast
from faz_a_fis_uretimi.generators.aciklama_uretici import ONCELIK_SIRASI, _belirleyici_turu_sec

# Tüm bilinen anomali türü isimleri (main.py'un ürettiği ANOMALI_FONKSIYONLARI
# ile ayni set) -- burada string olarak tutuluyor, çünkü bu script generators/
# paketine bağimli olmadan, salt JSON çiktisindan çalişabilsin istiyoruz.
BILINEN_ANOMALI_TURLERI = [
    "gelecek_tarihli", "gecersiz_kimlik_no", "kdv_kategori_uyumsuzlugu",
    "is_kolu_kategori_uyumsuzlugu", "yasakli_kategori", "limit_asimi",
    "ara_toplam", "kdv_tutari", "satir_toplami", "sistematik_yuvarlama",
    "ondalik_kaymasi", "dusuk_ondalik_kaymasi", "basamak_karisikligi",
    "genel_toplam", "footer_kismi",
    "mukerrer_fis_yukleme", "fatura_no_cakismasi",
    # NOT: "fahis_fiyat" / "dusuk_fiyat" BURADAN KALDIRILDI -- bu adlar
    # "ondalik_kaymasi" / "dusuk_ondalik_kaymasi" olarak YENİDEN ADLANDIRILDI
    # (eski ad enflasyon çağrışımı yapıyordu, oysa bu bir VERİ GİRİŞ HATASI;
    # bkz. generators/anomaly_injector.py ADLANDIRMA NOTU). Listede kaldıkları
    # sürece her koşuda "hiç üretilmemiş tür" uyarısı veriyorlardı.
]


def yukle(dosya_yolu: Path):
    if not dosya_yolu.exists():
        raise FileNotFoundError(f"Dosya bulunamadi: {dosya_yolu}")
    with open(dosya_yolu, "r", encoding="utf-8") as f:
        return json.load(f)


def rapor_uret(etiketler: list[dict], rapor: dict) -> None:
    toplam = len(etiketler)
    if toplam == 0:
        print("Etiket listesi boş, analiz yapilamiyor.")
        return

    anomalili_sayisi = sum(1 for e in etiketler if e["is_anomali"])
    gercek_oran = anomalili_sayisi / toplam

    hedef_oran = rapor.get("hedef_anomali_orani")
    talep_edilen = rapor.get("talep_edilen_fatura_adedi")
    elenen = rapor.get("elenen_fatura_sayisi_vkn_tutarsizlik", 0)

    print("=" * 60)
    print("  ANOMALİ ORANI KONTROLÜ")
    print("=" * 60)
    if hedef_oran is not None:
        sapma = gercek_oran - hedef_oran
        durum = "✓ OK" if abs(sapma) < 0.02 else "⚠️  SAPMA VAR"
        print(f"  Hedef oran   : {hedef_oran:.4f}")
        print(f"  Gerçek oran  : {gercek_oran:.4f}  (üretim SONRASI veri setine göre)")
        print(f"  Fark         : {sapma:+.4f}   {durum}")
        print(f"  (Not: gerçek oran, VKN/ad tutarsizligi filtresinden SONRAKI")
        print(f"   {toplam} faturaya göre hesaplandi -- kullaniciya giden veri budur)")
    else:
        print(f"  Hedef oran rapor.json içinde yok (eski bir rapor olabilir).")
        print(f"  Gerçek oran  : {gercek_oran:.4f}")

    if talep_edilen is not None:
        beklenen_toplam = talep_edilen - elenen
        durum = "✓ OK" if beklenen_toplam == toplam else "⚠️  UYUŞMUYOR"
        print(f"\n  Talep edilen adet        : {talep_edilen}")
        print(f"  VKN/ad tutarsizligi elenen: {elenen}")
        print(f"  Beklenen toplam (talep-elenen): {beklenen_toplam}")
        print(f"  Rapordaki toplam_fatura   : {toplam}   {durum}")

    print(f"\n  Toplam fatura      : {toplam}")
    print(f"  Anomalili fatura   : {anomalili_sayisi}")
    print(f"  Temiz fatura       : {toplam - anomalili_sayisi}")

    # Tür bazli dağilim
    tur_sayaci: Counter = Counter()
    coklu_anomali_sayisi = 0
    for e in etiketler:
        turler = e.get("anomali_turleri", [])
        tur_sayaci.update(turler)
        if len(turler) > 1:
            coklu_anomali_sayisi += 1

    print("\n" + "=" * 60)
    print("  ANOMALİ TÜRÜ DAĞILIMI")
    print("=" * 60)
    print(f"  (Bir faturada birden fazla tür olabilir -- union etiketleme.")
    print(f"   Birden fazla türü olan fatura sayisi: {coklu_anomali_sayisi})\n")

    if not tur_sayaci:
        print("  Hiç anomali üretilmemiş.")
    else:
        baslik = f"  {'TÜR':<32}{'ADET':>8}{'% TOPLAM':>12}{'% ANOMALİLİ':>14}"
        print(baslik)
        print("  " + "-" * 64)
        for tur, adet in tur_sayaci.most_common():
            yuzde_toplam = adet / toplam * 100
            yuzde_anomalili = adet / anomalili_sayisi * 100 if anomalili_sayisi else 0
            print(f"  {tur:<32}{adet:>8}{yuzde_toplam:>11.2f}%{yuzde_anomalili:>13.2f}%")

    # Hiç üretilmemiş türler
    uretilmemis = [t for t in BILINEN_ANOMALI_TURLERI if tur_sayaci.get(t, 0) == 0]
    if uretilmemis:
        print("\n  ⚠️  Hiç üretilmemiş / rastlanmamiş türler:")
        for t in uretilmemis:
            print(f"    - {t}")

    # rapor.json'daki politika/is-kolu ihlal sayilari (mevcutsa göster)
    print("\n" + "=" * 60)
    print("  POLİTİKA İHLALLERİ (rapor.json'dan)")
    print("=" * 60)
    for anahtar, etiket in [
        ("yasakli_kategori_sayisi", "Yasakli kategori"),
        ("limit_asimi_sayisi", "Limit aşimi"),
        ("ondalik_kaymasi_sayisi", "Ondalık kayması (yukarı)"),
        ("dusuk_ondalik_kaymasi_sayisi", "Ondalık kayması (aşağı)"),
        ("is_kolu_uyumsuzlugu_sayisi", "İş kolu-kategori uyumsuzluğu"),
    ]:
        if anahtar in rapor:
            print(f"  {etiket:<32}: {rapor[anahtar]}")

    print("=" * 60)

def aciklama_kategorisi_raporu(etiketler: list[dict]) -> None:
    """
    aciklama_kategorisi alaninin genel dağılımini VE anomali türüne göre
    kirilimini basar. Tür kirilimi icin her faturanin ONCELIK_SIRASI'na göre
    hangi türle belirlendiğini _belirleyici_turu_sec ile yeniden hesaplar --
    etiketler.json bu ara bilgiyi saklamiyor, burada tekrar türetiliyor.
    """
    if not etiketler or "aciklama_kategorisi" not in etiketler[0]:
        print("\n(aciklama_kategorisi alani etiketlerde yok -- bu üretim "
              "aciklama_uretici.py entegrasyonundan ÖNCE yapilmiş olabilir.)")
        return

    print("\n" + "=" * 60)
    print("  AÇIKLAMA KATEGORİSİ DAĞILIMI")
    print("=" * 60)

    genel_sayac = Counter(e["aciklama_kategorisi"] for e in etiketler)
    toplam = len(etiketler)
    print(f"\n  Genel dağılım ({toplam} fatura):")
    for kategori in ["yeterli", "yetersiz", "manipulatif", "ai_uretimi"]:
        adet = genel_sayac.get(kategori, 0)
        print(f"    {kategori:<14}: {adet:>7}  (%{adet / toplam * 100:.2f})")

    tur_gruplari: dict[str, Counter] = {}
    for e in etiketler:
        belirleyici = _belirleyici_turu_sec(e.get("anomali_turleri", []))
        tur_gruplari.setdefault(belirleyici, Counter())[e["aciklama_kategorisi"]] += 1

    print(f"\n  Tür bazli kirilim (belirleyici tür = ONCELIK_SIRASI'na göre en öncelikli tür):")
    baslik_sirasi = ["temiz"] + ONCELIK_SIRASI
    for tur in baslik_sirasi:
        if tur not in tur_gruplari:
            continue
        sayac = tur_gruplari[tur]
        alt_toplam = sum(sayac.values())
        satir = "  ".join(
            f"{k}=%{sayac.get(k, 0) / alt_toplam * 100:.0f}"
            for k in ["yeterli", "yetersiz", "manipulatif", "ai_uretimi"]
        )
        print(f"    {tur:<32} (n={alt_toplam:>5})  {satir}")

    bilinen = set(baslik_sirasi)
    for tur, sayac in tur_gruplari.items():
        if tur not in bilinen:
            alt_toplam = sum(sayac.values())
            satir = "  ".join(
                f"{k}=%{sayac.get(k, 0) / alt_toplam * 100:.0f}"
                for k in ["yeterli", "yetersiz", "manipulatif", "ai_uretimi"]
            )
            print(f"    {tur:<32} (n={alt_toplam:>5})  {satir}   [ONCELIK_SIRASI disinda]")

def main():
    parser = argparse.ArgumentParser(description="masrafAI üretilen veri seti kalite raporu")
    parser.add_argument("--output-dir", type=str, default="data", help="main.py'un çiktilarinin olduğu klasör")
    parser.add_argument("--filename", type=str, default="faturalar", help="main.py'a verilen --filename (uzantisiz)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    etiket_yolu = output_dir / f"{args.filename}_etiketler.json"
    rapor_yolu = output_dir / f"{args.filename}_rapor.json"

    etiketler = cast(list, yukle(etiket_yolu))
    rapor = cast(dict, yukle(rapor_yolu))

    rapor_uret(etiketler, rapor)
    aciklama_kategorisi_raporu(etiketler)


if __name__ == "__main__":
    main()