"""
Açıklama üretim teşhis/kalibrasyon aracı (Faz 5).

Pilot jsonl'ini VEYA runner batch çıktı JSON'larını okuyup kalite/çeşitlilik
teşhisi üretir. Amaç: kural eşiklerini göz kararı MD okuyarak değil, veriye
bakarak ayarlamak.

Raporladıkları:
  1. Kategori dağılımı
  2. Çeşitlilik: distinct-1/2, uzunluk istatistiği (kategori-uzunluk korelasyon sentineli)
  3. İhlal frekansı (hangi kural en çok RETRY SONRASI kalıyor -> ya çok katı ya gerçek sorun)
  4. Retry oranı (deneme_sayisi==2)
  5. Yakın-kopya oranı
  6. En sık tekrar eden tam-metin öbekler (mode collapse yüzeye çıkar)

Kullanım:
    # pilot jsonl
    python -m faz_b_aciklama_uretimi.aciklama_analiz --girdi deneme.jsonl
    # runner batch çıktıları (glob)
    python -m faz_b_aciklama_uretimi.aciklama_analiz --cikti-dizini data/aciklama
    # tek/çoklu dosya
    python -m faz_b_aciklama_uretimi.aciklama_analiz --girdi data/aciklama/batch_0001_ciktilar.json
"""

import argparse
import glob
import json
from collections import Counter
from pathlib import Path

from faz_b_aciklama_uretimi.aciklama_uretim_core import KATEGORILER, distinct_n, _dedup_normalize


# ---------------------------------------------------------------------------
# Yükleme: hem pilot jsonl (satır başına obje) hem runner çıktı (fatura_no->obje
# sözlüğü) desteklenir. Normalize edilmiş kayıt listesi döner.
# ---------------------------------------------------------------------------

def _kayitlari_normalize(nesne) -> list[dict]:
    if isinstance(nesne, dict):
        # runner çıktı: {fatura_no: {aciklama_metni, aciklama_kategorisi, ...}}
        kayitlar = []
        for fno, k in nesne.items():
            if not isinstance(k, dict):
                continue
            k = dict(k)
            k.setdefault("fatura_no", fno)
            kayitlar.append(k)
        return kayitlar
    if isinstance(nesne, list):
        return [k for k in nesne if isinstance(k, dict)]
    return []


def dosya_yukle(yol: Path) -> list[dict]:
    metin = yol.read_text(encoding="utf-8").strip()
    if not metin:
        return []
    # Önce jsonl dene (satır başına obje); olmazsa tek büyük JSON olarak yükle.
    satir_kayitlari = []
    coklu_satir = "\n" in metin
    if yol.suffix == ".jsonl" or coklu_satir:
        for satir in metin.splitlines():
            satir = satir.strip()
            if not satir:
                continue
            try:
                satir_kayitlari.append(json.loads(satir))
            except json.JSONDecodeError:
                satir_kayitlari = []
                break
    if len(satir_kayitlari) > 1:
        return _kayitlari_normalize(satir_kayitlari)
    return _kayitlari_normalize(json.loads(metin))


def girdileri_topla(args) -> list[dict]:
    yollar: list[Path] = []
    if args.cikti_dizini:
        yollar += [Path(p) for p in sorted(glob.glob(str(Path(args.cikti_dizini) / "batch_*_ciktilar.json")))]
    for g in args.girdi or []:
        eslesenler = sorted(glob.glob(g))
        yollar += [Path(p) for p in eslesenler] if eslesenler else [Path(g)]
    kayitlar: list[dict] = []
    for y in yollar:
        if not y.exists():
            print(f"[!] bulunamadı: {y}")
            continue
        yeni = dosya_yukle(y)
        print(f"[+] {y.name}: {len(yeni)} kayıt")
        kayitlar += yeni
    return kayitlar


# ---------------------------------------------------------------------------
# Rapor
# ---------------------------------------------------------------------------

def _kategori_grupla(kayitlar: list[dict]) -> dict[str, list[dict]]:
    grup: dict[str, list[dict]] = {}
    for k in kayitlar:
        grup.setdefault(k.get("aciklama_kategorisi", "?"), []).append(k)
    return grup


def _sirali_kategoriler(grup: dict) -> list[str]:
    bilinen = [k for k in KATEGORILER if k in grup]
    diger = [k for k in grup if k not in KATEGORILER]
    return bilinen + diger


def rapor_yaz(kayitlar: list[dict]) -> None:
    if not kayitlar:
        print("Kayıt yok.")
        return
    grup = _kategori_grupla(kayitlar)
    kats = _sirali_kategoriler(grup)
    toplam = len(kayitlar)

    print("\n" + "=" * 70)
    print(f"TOPLAM KAYIT: {toplam}")
    print("=" * 70)

    # 1) Kategori dağılımı
    print("\n[1] Kategori dağılımı:")
    for kat in kats:
        n = len(grup[kat])
        print(f"    {kat:12s} {n:5d}  (%{100*n/toplam:.1f})")

    # 2) Çeşitlilik + uzunluk (kategori-uzunluk korelasyon sentineli)
    print("\n[2] Çeşitlilik ve uzunluk (distinct 1'e yakın = çeşitli):")
    print(f"    {'kategori':12s} {'n':>5s} {'dist-1':>7s} {'dist-2':>7s} {'ort.krk':>8s} {'min':>4s} {'max':>4s} {'ort.kel':>8s}")
    ort_uzunluklar = {}
    for kat in kats:
        metinler = [k.get("aciklama_metni", "") for k in grup[kat] if k.get("aciklama_metni")]
        if not metinler:
            continue
        krk = [len(m) for m in metinler]
        kel = [len(m.split()) for m in metinler]
        ort_uzunluklar[kat] = sum(krk) / len(krk)
        print(f"    {kat:12s} {len(metinler):5d} {distinct_n(metinler,1):7.3f} {distinct_n(metinler,2):7.3f} "
              f"{ort_uzunluklar[kat]:8.1f} {min(krk):4d} {max(krk):4d} {sum(kel)/len(kel):8.1f}")
    if len(ort_uzunluklar) >= 2:
        en_uzun, en_kisa = max(ort_uzunluklar.values()), min(ort_uzunluklar.values())
        oran = en_uzun / en_kisa if en_kisa else 0
        durum = "⚠ YÜKSEK (uzunluk kategori sızdırabilir)" if oran > 3 else "iyi (uzunluk zayıf ayraç)"
        print(f"    -> kategoriler arası ort.uzunluk oranı: {oran:.2f}x  [{durum}]")

    # 3) İhlal frekansı (retry sonrası KALAN)
    print("\n[3] Retry sonrası kalan ihlal frekansı (yüksek = kural çok katı VEYA gerçek sorun):")
    ihlal_sayaci = Counter()
    ihlalli_kayit = 0
    for k in kayitlar:
        ihl = k.get("kalan_ihlaller") or []
        if ihl:
            ihlalli_kayit += 1
        ihlal_sayaci.update(ihl)
    if ihlal_sayaci:
        print(f"    kalan-ihlalli kayıt: {ihlalli_kayit}/{toplam} (%{100*ihlalli_kayit/toplam:.1f})")
        for ihl, s in ihlal_sayaci.most_common():
            print(f"      {ihl:22s} {s:5d}  (%{100*s/toplam:.1f})")
    else:
        print("    (kalan ihlal yok)")

    # 4) Retry oranı
    print("\n[4] Retry oranı (deneme_sayisi==2):")
    for kat in kats:
        ks = grup[kat]
        retry = sum(1 for k in ks if k.get("deneme_sayisi") == 2)
        if ks:
            print(f"    {kat:12s} {retry:5d}/{len(ks):<5d} (%{100*retry/len(ks):.1f})")

    # 5) Yakın-kopya oranı
    print("\n[5] Yakın-kopya oranı (yakin_kopya=True):")
    if not any("yakin_kopya" in k for k in kayitlar):
        print("    (kayıtlarda yakin_kopya alanı yok)")
    else:
        for kat in kats:
            ks = grup[kat]
            yk = sum(1 for k in ks if k.get("yakin_kopya"))
            if ks:
                print(f"    {kat:12s} {yk:5d}/{len(ks):<5d} (%{100*yk/len(ks):.1f})")

    # 6) En sık tekrar eden tam-metin öbekler (mode collapse yüzeye çıkar)
    print("\n[6] En sık tekrarlanan açıklamalar (tekrar>1 -> collapse işareti):")
    hic_tekrar = True
    for kat in kats:
        sayac = Counter(_dedup_normalize(k.get("aciklama_metni", "")) for k in grup[kat] if k.get("aciklama_metni"))
        tekrarli = [(m, s) for m, s in sayac.most_common(5) if s > 1]
        if not tekrarli:
            continue
        hic_tekrar = False
        print(f"    [{kat}]")
        for m, s in tekrarli:
            print(f"      {s:3d}x  {m[:70]}")
    if hic_tekrar:
        print("    (kategori içi birebir tekrar yok — iyi)")

    print()


def main():
    parser = argparse.ArgumentParser(description="Açıklama üretim teşhis/kalibrasyon aracı (Faz 5)")
    parser.add_argument("--girdi", nargs="*", help="pilot jsonl ya da çıktı JSON dosyaları (glob desteklenir)")
    parser.add_argument("--cikti-dizini", default=None, help="runner çıktı dizini (batch_*_ciktilar.json otomatik toplanır)")
    args = parser.parse_args()

    if not args.girdi and not args.cikti_dizini:
        parser.error("--girdi veya --cikti-dizini gerekli")

    kayitlar = girdileri_topla(args)
    rapor_yaz(kayitlar)


if __name__ == "__main__":
    main()
