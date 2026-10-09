"""
İKİ AYRI KOŞU arasında YAKIN KOPYA denetimi (birleştirmeden ÖNCE koşulur).

NEDEN GEREKLİ: `aciklama_toplu_uret.py`'nin dedup birikimi (`kabul_token_setleri`)
süreç başında BOŞ başlar ve yalnız kendi dizinindeki çıktı dosyalarından beslenir.
Ek bir koşu YENİ bir dizinde çalıştığında önceki koşunun metinlerini GÖRMEZ:
üretilen metin eskilerden birinin kopyası olsa bile `yakin_kopya` bayrağı FALSE
kalır ve `aciklama_birlestir.py` onu temiz sanır.

Bu modül yeni koşunun kayıtlarını TABAN koşuya karşı denetler ve yalnız YENİ
dosyalara bayrak basar. Taban dizine DOKUNULMAZ.

EŞİK runner ile BİREBİR AYNI olmak ZORUNDA: `yetersiz` 0.95, diğerleri 0.80.
`yetersiz` metinleri kısa olduğu için 0.80'de doğal olarak çarpışır; eşiği
düşürmek kategorinin yarısını sahte kopya diye işaretlerdi.

BİRİKİM SIRASI runner'ı taklit eder: kabul edilen her yeni metin, sonraki
karşılaştırmalara DA katılır (yoksa yeni koşu kendi içinde kopya üretir).

    # rapor (yazmaz)
    python -m faz_b_aciklama_uretimi.aciklama_global_dedup --taban-dizin data/ciktilar_25k-4 \
        --yeni-dizin data/aciklama_yetersiz_ek

    # bayrakları yaz
    python -m faz_b_aciklama_uretimi.aciklama_global_dedup --taban-dizin data/ciktilar_25k-4 \
        --yeni-dizin data/aciklama_yetersiz_ek --uygula
"""

import argparse
import glob
import json
import shutil
from collections import defaultdict
from pathlib import Path

from faz_b_aciklama_uretimi.aciklama_uretim_core import _token_set, jaccard

# runner ile AYNI olmalı (aciklama_toplu_uret.py: dedup_esik satırı)
ESIK = {"yetersiz": 0.95}
ESIK_VARSAYILAN = 0.80


def _cikti_dosyalari(dizin: str) -> list[Path]:
    return sorted(Path(p) for p in glob.glob(str(Path(dizin) / "batch_*_ciktilar.json")))


def _taban_yukle(dizin: str) -> dict[str, list[set[str]]]:
    """Taban koşudaki TÜM metinlerin token kümeleri (runner'ın birikimiyle aynı:
    ihlalli/kopya ayırmadan hepsi katılır)."""
    birikim: dict[str, list[set[str]]] = defaultdict(list)
    for yol in _cikti_dosyalari(dizin):
        for v in json.loads(yol.read_text(encoding="utf-8")).values():
            metin = v.get("aciklama_metni")
            if metin:
                birikim[v["aciklama_kategorisi"]].append(_token_set(metin))
    return birikim


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--taban-dizin", required=True,
                    help="önceki koşunun çıktı dizini (DEĞİŞTİRİLMEZ)")
    ap.add_argument("--yeni-dizin", required=True,
                    help="bayrak basılacak yeni koşunun çıktı dizini")
    ap.add_argument("--uygula", action="store_true",
                    help="yedek alıp yeni dosyalara yaz (varsayılan: sadece rapor)")
    a = ap.parse_args()

    yeni_dosyalar = _cikti_dosyalari(a.yeni_dizin)
    if not yeni_dosyalar:
        print(f"UYARI: {a.yeni_dizin} altında *_ciktilar.json yok. "
              f"Önce üretim koşusunu tamamla.")
        return

    birikim = _taban_yukle(a.taban_dizin)
    print(f"taban birikim: " + ", ".join(f"{k}={len(v)}" for k, v in sorted(birikim.items())))

    toplam = yeni_bayrak = zaten_bayrakli = 0
    ornekler: list[str] = []

    for yol in yeni_dosyalar:
        veri = json.loads(yol.read_text(encoding="utf-8"))
        degisti = False
        for v in veri.values():
            metin = v.get("aciklama_metni")
            if not metin:
                continue
            toplam += 1
            kategori = v["aciklama_kategorisi"]
            esik = ESIK.get(kategori, ESIK_VARSAYILAN)
            tok = _token_set(metin)
            if v.get("yakin_kopya"):
                zaten_bayrakli += 1
            elif any(jaccard(tok, var) >= esik for var in birikim[kategori]):
                v["yakin_kopya"] = True
                yeni_bayrak += 1
                degisti = True
                if len(ornekler) < 6:
                    ornekler.append(metin)
            # runner gibi: kabul edilsin ya da edilmesin birikime katılır
            birikim[kategori].append(tok)
        if degisti and a.uygula:
            shutil.copy2(yol, yol.with_suffix(yol.suffix + ".yedek_dedup"))
            yol.write_text(json.dumps(veri, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    print(f"\nyeni kayıt         : {toplam}")
    print(f"koşu içinde bayraklı: {zaten_bayrakli}")
    print(f"TABANA karşı YENİ bayrak: {yeni_bayrak}")
    kalan = toplam - zaten_bayrakli - yeni_bayrak
    print(f"kopya olmayan       : {kalan}")
    if ornekler:
        print("\n--- taban ile çakışan örnekler ---")
        for m in ornekler:
            print(f"   {m[:88]}")
    print("\n" + ("YAZILDI (yedekler *.yedek_dedup)" if a.uygula
                  else "RAPOR modu; yazmak için --uygula"))


if __name__ == "__main__":
    main()
