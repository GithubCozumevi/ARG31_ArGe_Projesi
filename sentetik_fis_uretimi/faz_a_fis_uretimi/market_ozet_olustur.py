"""
Ham kasa dökümünü (603.545 satır) özet forma (9.262 satır + `siklik` kolonu)
indirger.

NEDEN: ham dosya bir katalog değil SATIŞ kaydı; aynı ürün defalarca geçiyor
(EKMEK 250 GR 9.122 kez, medyan tekrar 12). Tekrar örtük bir frekans ağırlığı
ve GERÇEKÇİLİK kaynağı: ekmek sık, kavun turşusu seyrek seçiliyor. Bu yüzden
tekrar SİLİNMEZ, `siklik` kolonuna TAŞINIR -- yükleyici geri açtığı için
üretim davranışı bit bit aynı kalır (bkz. field_generator._market_satirlari).

Kazanç: dosya 65 kat küçülür ve `alt_tipler` kolonu (satıcı alt tipi başına
izinli ürün) doğal yerini bulur; etiket tablosu ile üretim tablosu AYNI dosya
olur, ayrı tutulsa ürün adı değiştiğinde eşleşme sessizce kopardı.

2026-08-21: özet dosya artık `data/urun_verileri/market_urunleri.csv` adını
TAŞIYOR (üretim onu doğrudan okuyor, ayrı bir "_ozet" dosyası yok). Ham kasa
dökümü `data/backups/urun_verileri/market_urunleri_yedek.csv`'ye arşivlendi
-- yeniden özetlemek gerekirse `--girdi` ile o yola işaret et.

Kullanım:
    python -m faz_a_fis_uretimi.market_ozet_olustur --girdi data/backups/urun_verileri/market_urunleri_yedek.csv
    python -m faz_a_fis_uretimi.market_ozet_olustur --dogrula  # yalnız karşılaştır, yazma
"""

import argparse
import csv
from collections import Counter, defaultdict
from pathlib import Path

HAM_CSV = Path("data/backups/urun_verileri/market_urunleri_yedek.csv")
OZET_CSV = Path("data/urun_verileri/market_urunleri.csv")
OZET_KOLONLARI = ["urun_adi", "csv_kategori", "siklik", "alt_tipler"]


def ham_oku(dosya: Path) -> list[tuple[str, str]]:
    """(urun_adi, csv_kategori) çiftleri, HAM hâliyle (ad temizliği yükleyicinin işi)."""
    with open(dosya, encoding="utf-8-sig") as f:
        return [(satir["ITEMNAME"].strip(), (satir.get("CATEGORY_NAME1") or "").strip())
                for satir in csv.DictReader(f)
                if (satir.get("ITEMNAME") or "").strip()]


def ozet_kur(ciftler: list[tuple[str, str]]) -> list[dict]:
    sayac = Counter(ciftler)
    # Ölçüldü: hiçbir ürün adı birden fazla kategoride geçmiyor, yani (ad, kategori)
    # ile ad aynı sayıda benzersiz. Yine de sözleşmeyi burada denetliyoruz.
    kategoriler: dict[str, set[str]] = defaultdict(set)
    for ad, kategori in sayac:
        kategoriler[ad].add(kategori)
    cakisan = [ad for ad, k in kategoriler.items() if len(k) > 1]
    if cakisan:
        print(f"[!] {len(cakisan)} ürün adı birden fazla kategoride geçiyor, "
              f"her (ad, kategori) çifti ayrı satır olacak. Örnek: {cakisan[:3]}")
    return [{"urun_adi": ad, "csv_kategori": kategori, "siklik": adet, "alt_tipler": ""}
            for (ad, kategori), adet in sorted(sayac.items(), key=lambda x: -x[1])]


def mevcut_alt_tipleri_koru(satirlar: list[dict], eski: Path) -> int:
    """Yeniden üretimde elle girilmiş `alt_tipler` etiketleri KAYBOLMAZ."""
    if not eski.exists():
        return 0
    with open(eski, encoding="utf-8-sig") as f:
        onceki = {(s["urun_adi"], s.get("csv_kategori", "")): (s.get("alt_tipler") or "")
                  for s in csv.DictReader(f)}
    korunan = 0
    for satir in satirlar:
        etiket = onceki.get((satir["urun_adi"], satir["csv_kategori"]), "")
        if etiket:
            satir["alt_tipler"] = etiket
            korunan += 1
    return korunan


def main():
    parser = argparse.ArgumentParser(description="Market CSV özetleyici (tekrar -> siklik)")
    parser.add_argument("--girdi", type=Path, default=HAM_CSV)
    parser.add_argument("--cikti", type=Path, default=OZET_CSV)
    parser.add_argument("--dogrula", action="store_true", help="yazma, yalnız karşılaştır")
    args = parser.parse_args()

    ciftler = ham_oku(args.girdi)
    satirlar = ozet_kur(ciftler)
    toplam_siklik = sum(s["siklik"] for s in satirlar)
    print(f"[+] {args.girdi}: {len(ciftler)} satır -> {len(satirlar)} benzersiz "
          f"(siklik toplamı {toplam_siklik})")
    if toplam_siklik != len(ciftler):
        raise SystemExit("[X] siklik toplamı ham satır sayısını tutmuyor, yazılmadı.")

    if args.dogrula:
        print("[i] --dogrula: dosya yazılmadı.")
        return

    korunan = mevcut_alt_tipleri_koru(satirlar, args.cikti)
    args.cikti.parent.mkdir(parents=True, exist_ok=True)
    with open(args.cikti, "w", newline="", encoding="utf-8") as f:
        yazici = csv.DictWriter(f, fieldnames=OZET_KOLONLARI)
        yazici.writeheader()
        yazici.writerows(satirlar)
    print(f"[+] {args.cikti} yazıldı" + (f" ({korunan} alt tip etiketi korundu)" if korunan else ""))

    kategori_sayaci = Counter(s["csv_kategori"] for s in satirlar)
    for kategori, adet in kategori_sayaci.most_common():
        print(f"    {kategori:22s} {adet:5d} benzersiz")


if __name__ == "__main__":
    main()
