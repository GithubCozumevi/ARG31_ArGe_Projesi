"""
Üretilmiş açıklamalardaki İYELİK EKİ + AYRILMA EKİ hatasını LLM ÇAĞRISI OLMADAN
düzeltir: "Fırat Oteli'den" -> "Fırat Oteli'nden".

NEDEN POST-HOC DÜZELTME MEŞRU: ek, Türkçede tamamen kurallıdır ve metnin
anlamına dokunmaz. `ayrilma_eki()` bu eki zaten prompt'a HAZIR veriyordu; kural
eksikti (bkz. `_IYELIK_SONU`, 2026-08-06). Yani düzeltme modelin yazdığı bir şeyi
değil, bizim ona yanlış verdiğimiz bir dizgiyi onarır. Yeniden üretim GEREKMEZ.

ÖLÇÜLDÜ (data/ciktilar_25k-4, ihlalsiz küme): hatalı 504, doğru 8.

Kapsam SINIRLI ve KASITLI: yalnız `_IYELIK_SONU` kümesindeki son kelimeler
düzeltilir. 'Taksi'den', 'Şarküteri'den', 'Teknoloji'den' DOĞRUdur, elleme.

Varsayılan RAPOR modu (urun_kategori_duzelt.py ile aynı kalıp); `--uygula`
yedek alıp yazar.

    python -m faz_b_aciklama_uretimi.aciklama_ek_duzelt --cikti-dizini data/ciktilar_25k-4
    python -m faz_b_aciklama_uretimi.aciklama_ek_duzelt --cikti-dizini data/ciktilar_25k-4 --uygula
"""

import argparse
import json
import re
import shutil
from pathlib import Path

from faz_b_aciklama_uretimi.aciklama_uretim_core import _iyelik_sonu_mu, _KALIN_SESLI, _SESLI

# Kesme işareti iki biçimde de geçebilir (düz ' ve tipografik ’).
_KESME = "['’]"
# Son kelimeyi yakala: harf dizisi + kesme + (d|t)(a|e)n. Kaynaştırma 'n'si YOKsa
# eşleşir; varsa (ndan/nden) `(?!n)` ile ELENİR -> zaten doğru olanlara dokunulmaz.
_DESEN = re.compile(
    rf"(\b[\wçğıöşüÇĞİÖŞÜ]+){_KESME}(?!n)([dt])([ae])n\b",
    re.UNICODE,
)


def _iyelik_mi(kelime: str) -> bool:
    return _iyelik_sonu_mu(kelime)


def _duzelt_metin(metin: str) -> tuple[str, int]:
    """Metindeki hatalı ekleri onarır. Dönüş: (yeni_metin, düzeltme_sayısı)."""
    sayac = 0

    def _degistir(m: re.Match) -> str:
        nonlocal sayac
        kelime = m.group(1)
        if not _iyelik_mi(kelime):
            return m.group(0)          # dokunma: 'Taksi'den' doğrudur
        sayac += 1
        harfler = [h for h in kelime.lower() if h.isalpha()]
        son_unlu = next((h for h in reversed(harfler) if h in _SESLI), "a")
        return f"{kelime}'n{'da' if son_unlu in _KALIN_SESLI else 'de'}n"

    return _DESEN.sub(_degistir, metin), sayac


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cikti-dizini", default="data/ciktilar_25k-4")
    ap.add_argument("--uygula", action="store_true",
                    help="yedek alıp dosyaları YENİDEN YAZ (varsayılan: sadece rapor)")
    a = ap.parse_args()

    dosyalar = sorted(Path(a.cikti_dizini).glob("*_ciktilar.json"))
    if not dosyalar:
        print(f"UYARI: {a.cikti_dizini} altında *_ciktilar.json yok.")
        return

    toplam_kayit = toplam_duzeltme = etkilenen = 0
    ornekler: list[tuple[str, str]] = []

    for yol in dosyalar:
        veri = json.loads(yol.read_text(encoding="utf-8"))
        degisti = False
        for kayit in veri.values():
            toplam_kayit += 1
            eski = kayit.get("aciklama_metni") or ""
            yeni, n = _duzelt_metin(eski)
            if n:
                toplam_duzeltme += n
                etkilenen += 1
                degisti = True
                if len(ornekler) < 8:
                    ornekler.append((eski, yeni))
                kayit["aciklama_metni"] = yeni
        if degisti and a.uygula:
            shutil.copy2(yol, yol.with_suffix(yol.suffix + ".yedek"))
            yol.write_text(json.dumps(veri, ensure_ascii=False, indent=2),
                           encoding="utf-8")

    print(f"dosya: {len(dosyalar)} | kayıt: {toplam_kayit}")
    print(f"düzeltilen metin: {etkilenen} (%{100 * etkilenen / max(toplam_kayit, 1):.2f})")
    print(f"toplam ek düzeltmesi: {toplam_duzeltme}")
    print("\n--- örnekler ---")
    for eski, yeni in ornekler:
        print(f"  ESKİ: {eski[:96]}")
        print(f"  YENİ: {yeni[:96]}\n")
    print("YAZILDI (yedekler *.yedek)" if a.uygula else "RAPOR modu; yazmak için --uygula")


if __name__ == "__main__":
    main()
