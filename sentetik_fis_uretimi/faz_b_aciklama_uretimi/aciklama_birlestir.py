"""
SON adım: tüm batch çıktı JSON'larını okuyup fatura_no -> aciklama_metni
haritası kurar, ardından asıl faturalar.json'a bu alanı ekleyerek eğitim
verisini (faturalar_aciklamali.json) üretir. Ollama kapalıyken çalıştırılır,
RAM sorunu yoktur.

Not: main.py:fatura_to_dict şu an aciklama_metni'ni export etmiyor; üretilen
metni eğitim girdisine katan yer burasıdır.

Kullanım:
    python -m faz_b_aciklama_uretimi.aciklama_birlestir
"""

import argparse
import glob
import json
from collections import Counter
from pathlib import Path

from faz_b_aciklama_uretimi.aciklama_uretim_core import elenmeli_mi, iliskisel_cift_idleri

VARSAYILAN_CIKTI_DIZINI = "data/aciklama"


def aciklama_haritasi_kur(dizin: Path, eleme: bool = False,
                          korunanlar: set[str] | None = None) -> tuple[dict[str, str], Counter]:
    """batch_*_ciktilar.json dosyalarından kayit_id -> aciklama_metni haritası.

    Anahtar fatura_no DEĞİL: mukerrer_fis_yukleme / fatura_no_cakismasi anomalilerinde
    ayni fatura_no iki kayitta bulunur ve her birinin KENDİ açiklamasi vardir.

    KALİTE SÜZGECİ varsayılan KAPALIDIR; `--eleme` ile açılır. Açıldığında
    `aciklama_uretim_core.elenmeli_mi` uygulanır: retry'dan sağ çıkan
    şema-sızıntısı ihlalleri ve kategori-kavramı sızıntısı taşıyan kayıtlar
    veri setine hiç girmez.

    `enum_sizinti`'nin `yeterli`/`manipulatif`te yanlış-pozitif olması
    (kalemin `harcama_kategorisi`'si zaten model girdisinde) artık kuralın
    kendisinde çözülü: o ihlal `ELEME_IHLALLERI_KATEGORILI` ile yalnız
    `yetersiz`te eler. Kayda `aciklama_kategorisi` yazılmamış eski çıktı
    dosyalarında eski davranış (her kategoride eler) sürer.
    Ölçüldü (25k): tam eleme 1.009 kayıt, kategori duyarlı eleme 122.

    Prompt'a kural eklemek yerine burada elemenin gerekçesi: yeni prompt
    kuralı retry maliyeti ve çeşitlilik kaybı getiriyor (docs/arsiv/faz-b-prompt.md
    §15), eleme ise sıfır maliyetli ve geri alınabilir. Silinen kayıt = o
    faturanın açıklaması YOK demektir; `--sadece-uretilenler` ile birlikte
    kullanıldığında fatura da nihai veri setine girmez."""
    harita: dict[str, str] = {}
    elenen = Counter()
    for yol in sorted(glob.glob(str(dizin / "batch_*_ciktilar.json"))):
        with open(yol, "r", encoding="utf-8") as f:
            cikti = json.load(f)
        for kid, kayit in cikti.items():
            metin = kayit["aciklama_metni"]
            if eleme and not (korunanlar and kid in korunanlar):
                at, sebep = elenmeli_mi(metin, kayit.get("kalan_ihlaller"),
                                        kayit.get("aciklama_kategorisi"))
                if at:
                    elenen[sebep] += 1
                    continue
            harita[kid] = metin
    return harita, elenen


def main():
    parser = argparse.ArgumentParser(description="Üretilen açıklamaları faturalar.json'a merge et")
    parser.add_argument("--cikti-dizini", default=VARSAYILAN_CIKTI_DIZINI, help="batch çıktı dizini")
    parser.add_argument("--input-json", default="data/faturalar.json", help="asıl faturalar.json")
    parser.add_argument("--etiket-json", default="data/faturalar_etiketler.json",
                        help="ilişkisel çift korumasını kurmak için (yalnız --eleme ile okunur)")
    parser.add_argument("--output-json", default="data/faturalar_aciklamali.json", help="çıktı dosyası")
    parser.add_argument("--sadece-uretilenler", action="store_true",
                        help="Sadece açıklaması üretilmiş faturaları yaz (alt küme çalıştıysa küçük dosya)")
    # DENEYSEL / VARSAYILAN KAPALI -- bkz. aciklama_haritasi_kur docstring'i.
    parser.add_argument("--eleme", action="store_true",
                        help="[DENEYSEL] Kalite süzgecini AÇ: şema sızıntılı kayıtları veri setine sokma")
    args = parser.parse_args()

    print(f"[+] {args.input_json} okunuyor...")
    with open(args.input_json, "r", encoding="utf-8") as f:
        faturalar = json.load(f)

    # İlişkisel çiftin iki üyesi de elemeden MUAF: biri düşerse anomali
    # çözülemez hale gelir ve o örnek için harcanan üretim boşa gider.
    korunanlar: set[str] = set()
    if args.eleme:
        with open(args.etiket_json, "r", encoding="utf-8") as f:
            etiket_map = {e["kayit_id"]: e for e in json.load(f)}
        korunanlar = iliskisel_cift_idleri(faturalar, etiket_map)
        print(f"[+] İlişkisel çift koruması: {len(korunanlar)} kayıt elemeden muaf.")

    dizin = Path(args.cikti_dizini)
    harita, elenen = aciklama_haritasi_kur(dizin, eleme=args.eleme, korunanlar=korunanlar)
    toplam_ham = len(harita) + sum(elenen.values())
    print(f"[+] {toplam_ham} adet üretilmiş açıklama bulundu.")
    if elenen:
        oran = sum(elenen.values()) / toplam_ham * 100 if toplam_ham else 0
        print(f"[!] KALİTE SÜZGECİ: {sum(elenen.values())} kayıt elendi (%{oran:.1f}) -> geriye {len(harita)}")
        for sebep, adet in elenen.most_common():
            print(f"      {sebep:34s} {adet}")

    if not harita:
        print("HATA: hiç üretilmiş açıklama yok. Önce aciklama_toplu_uret.py çalıştır.")
        return

    eslesen = 0
    sonuc = []
    for fatura in faturalar:
        metin = harita.get(fatura["kayit_id"])
        if metin is not None:
            fatura["aciklama_metni"] = metin
            eslesen += 1
            sonuc.append(fatura)
        elif not args.sadece_uretilenler:
            fatura["aciklama_metni"] = ""
            sonuc.append(fatura)

    with open(args.output_json, "w", encoding="utf-8") as f:
        # indent=2: faturalar.json ile aynı biçim (main.py) -- nihai çıktı elle
        # incelenen dosya, tek satır olunca editörde açmak zorlaşıyor.
        json.dump(sonuc, f, ensure_ascii=False, indent=2)

    print(f"[+] {eslesen} faturaya aciklama_metni eklendi.")
    print(f"[+] Toplam {len(sonuc)} fatura yazıldı -> {args.output_json}")
    if eslesen < len(harita):
        print(f"[!] UYARI: {len(harita) - eslesen} açıklamanın kayit_id'si faturalar.json'da eşleşmedi.")


if __name__ == "__main__":
    main()
