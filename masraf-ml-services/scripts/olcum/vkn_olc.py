"""
VKN cikarim akisinin gercek fislerde olculmesi (leave-one-out).

Uc yontemi karsilastirir:
  A) Yalniz OCR            (regex + checksum + tek hane duzeltme)
  B) OCR + VLM             (VLM'in VKN'si checksum'i geciyorsa kullanilir)
  C) OCR + VLM + Sozluk    (satici sozlugu)

C'de her fis icin sozluk, O FISIN KENDISI CIKARILARAK yeniden kurulur; boylece
sozluk, olculen fisin cevabini onceden gormez (sizinti olmaz).

Kullanim (proje kok klasorunden, venv aktifken):
  python -m scripts.olcum.vkn_olc --gold "C:\\...\\json3" --ocr "C:\\...\\ocr" --vlm "C:\\...\\cleaned"

  --gold : gold JSON klasoru ({fis}.json, seller_tax_id + company)
  --ocr  : OCR JSON klasoru  ({fis}.json, "ocr": [{"text": ...}, ...])
  --vlm  : VLM cikti klasoru ({fis}_clean.json ya da {fis}_pred.json) - opsiyonel
  --mlflow : sonuclari MLflow'a da kaydet (opsiyonel)

Cikti: ekranda ozet tablo + data\\vkn_olcum.csv (fis bazinda sonuclar)
"""
import argparse
import csv
import os
import re
from collections import Counter

from dotenv import load_dotenv

load_dotenv()

from app.services.extraction import is_valid_tckn_checksum, is_valid_vkn_checksum, normalize_sayi
from app.services.pipeline import vkn_karar_ver
from app.services.vendors import sozluk_kur, tek_hane_varyantlari, vkn_adaylari
from scripts.ortak import gold_dosyalari, kayma_argumanlari_ekle, kayma_cozucu, ocr_metni, veri_yolu, vlm_oku

YONTEM_ADLARI = ["A) Yalniz OCR", "B) OCR + VLM", "C) OCR + VLM + Sozluk"]


def hata_nedeni(gold, metin):
    """Yanlis cikan bir fiste dogru numaranin OCR metninde ne durumda oldugunu siniflandirir."""
    if len(gold) == 11:  # TCKN
        if gold in re.findall(r"(?<!\d)\d{11}(?!\d)", metin):
            return "dogru numara metinde VAR ama baska numara secildi"
        return "dogru numara metinde yok (OCR okuyamadi ya da gold farkli)"
    adaylar = vkn_adaylari(metin)
    if gold in adaylar:
        return "dogru numara metinde VAR ama baska numara secildi"
    if any(gold in tek_hane_varyantlari(x) for x in adaylar):
        return "OCR dogru numarayi tek hane yanlis okudu"
    if not adaylar:
        return "metinde hic 10 haneli sayi yok"
    if not is_valid_vkn_checksum(gold):
        return "gold VKN checksum'i gecmiyor (gold hatasi olabilir)"
    return "dogru numara metinde yok (OCR okuyamadi ya da gold farkli)"


def gold_oku(gold_klasoru):
    """
    Olculecek fisleri gold klasorunden secer.
    Dondurur: (fisler [(fis, gold_vkn, gold_firma)], atlananlar {tckn, gecersiz_11_hane, vkn_yok})
    """
    fisler = []
    sayac = {"tckn": 0, "gecersiz_11_hane": 0, "vkn_yok": 0}
    for fis, g in gold_dosyalari(gold_klasoru):
        vkn = normalize_sayi(str(g.get("seller_tax_id") or ""))
        firma = str(g.get("company") or "").strip()
        if re.fullmatch(r"\d{10}", vkn):
            fisler.append((fis, vkn, firma))
        elif re.fullmatch(r"\d{11}", vkn):
            if is_valid_tckn_checksum(vkn):
                fisler.append((fis, vkn, firma))   # sahis saticilar (TCKN) da olculur
                sayac["tckn"] += 1
            else:
                sayac["gecersiz_11_hane"] += 1     # checksum'u gecmeyen 11 haneli: etiket hatasi olabilir
        else:
            sayac["vkn_yok"] += 1
    return fisler, sayac


def fisleri_olc(fisler, ocr_klasoru, vlm_klasoru, ocr_adi):
    """
    Her fis icin A/B/C yontemlerini calistirir.
    Dondurur: (sonuclar [(fis, gold_vkn, A, B, C, ocr_metni)], ocr_dosyasi_olmayan_fis_sayisi)
    """
    tum_ciftler = [(v, f) for _, v, f in fisler if f]
    sonuclar, ocr_yok = [], 0
    for fis, gold_vkn, gold_firma in fisler:
        metin = ocr_metni(os.path.join(ocr_klasoru, ocr_adi(fis) + ".json"))
        if metin is None:
            ocr_yok += 1
            continue
        vlm_vkn, vlm_firma = vlm_oku(vlm_klasoru, ocr_adi(fis))

        # Leave-one-out sozluk: bu fisin kendi (vkn, firma) cifti cikarilir
        ciftler = list(tum_ciftler)
        if (gold_vkn, gold_firma) in ciftler:
            ciftler.remove((gold_vkn, gold_firma))
        sozluk = sozluk_kur(ciftler)

        A = vkn_karar_ver(metin)
        B = vkn_karar_ver(metin, vlm_vkn)
        C = vkn_karar_ver(metin, vlm_vkn, sozluk=sozluk, firma_ipucu=vlm_firma)
        sonuclar.append((fis, gold_vkn, A, B, C, metin))
    return sonuclar, ocr_yok


def yontem_ozeti(sonuclar, idx):
    """idx: 0=A, 1=B, 2=C. Dogru / yanlis / bos sayilari ve otomatik kabul edilenlerin dogrulugu."""
    dogru = yanlis = bos = oto = oto_dogru = 0
    for _, gold, *r in sonuclar:
        s = r[idx]
        if s["vkn"] is None:
            bos += 1
        elif s["vkn"] == gold:
            dogru += 1
        else:
            yanlis += 1
        if not s["insan_kontrolu"]:
            oto += 1
            if s["vkn"] == gold:
                oto_dogru += 1
    return dict(dogru=dogru, yanlis=yanlis, bos=bos, oto=oto, oto_dogru=oto_dogru)


def ozet_yazdir(n, ozetler, sonuclar, sayac, ocr_yok, vlm_verildi):
    print(f"\nOlculen fis: {n}   (bunlardan TCKN/sahis: {sayac['tckn']}; checksum'u gecmeyen 11 haneli etiket (olculmedi): "
          f"{sayac['gecersiz_11_hane']},gold'da VKN yok: {sayac['vkn_yok']}, OCR dosyasi yok: {ocr_yok})")
    print(f"VLM klasoru: {'verildi' if vlm_verildi else 'verilmedi (B = A)'}\n")
    basl = f"{'Yontem':<24}{'Dogru':>10}{'Yanlis':>10}{'Bos':>8}{'Otomatik kabul':>18}{'Otomatik dogruluk':>20}"
    print(basl)
    print("-" * len(basl))
    for ad, o in zip(YONTEM_ADLARI, ozetler):
        oto_orani = o["oto_dogru"] / o["oto"] if o["oto"] else 0.0
        print(f"{ad:<24}{o['dogru']:>5} %{100*o['dogru']/n:>3.0f}{o['yanlis']:>6} %{100*o['yanlis']/n:>2.0f}"
              f"{o['bos']:>8}{o['oto']:>10} %{100*o['oto']/n:>3.0f}{oto_orani*100:>16.1f}%")

    sozluk_kurtarilan = sum(1 for _, g, A, B, C, _m in sonuclar if C["kaynak"] == "satici_sozlugu" and C["vkn"] == g)
    sozluk_yanlis = sum(1 for _, g, A, B, C, _m in sonuclar if C["kaynak"] == "satici_sozlugu" and C["vkn"] != g)
    uyusmuyor = sum(1 for _f, _g, _a, _b, C, _m in sonuclar if C["satici"].get("firma_uyusmuyor"))
    print(f"\nSozlugun devreye girdigi fis: dogru {sozluk_kurtarilan}, yanlis {sozluk_yanlis}")
    print(f"'VKN ayni, firma adi farkli' uyarisi verilen fis: {uyusmuyor}")
    print("\nNot: 'Dogru/Yanlis/Bos' tum fisler uzerinden. 'Otomatik dogruluk' = insan kontrolune")
    print("gonderilmeden kabul edilen sonuclar icindeki dogruluk (asil guvenilirlik olcusu).")


def hata_analizi_yazdir(sonuclar):
    """Insan kontrolune gitmeden kabul edilen ama YANLIS olan fisler (sessiz hatalar) ve nedenleri."""
    sessiz = [(f, g, C, m) for f, g, A, B, C, m in sonuclar
              if C["vkn"] is not None and C["vkn"] != g and not C["insan_kontrolu"]]
    print(f"\n=== HATA ANALIZI: C yonteminde otomatik kabul edilip YANLIS olan {len(sessiz)} fis ===")
    nedenler = Counter(hata_nedeni(g, m) for _, g, _, m in sessiz)
    for neden, adet in nedenler.most_common():
        print(f"  {adet:>3}  {neden}")

    coklu = sum(1 for _, g, C, m in sessiz
                if len([x for x in vkn_adaylari(m) if is_valid_vkn_checksum(x)]) >= 2)
    print(f"\n  Bu fislerin {coklu} tanesinde metinde checksum'i gecen BIRDEN FAZLA 10 haneli numara var")

    kaynaklar = Counter(C["kaynak"] for _, _, C, _ in sessiz)
    print("  Yanlisin geldigi kaynak:", dict(kaynaklar))

    print("\n  Ornekler (fis | gercek VKN | secilen VKN | kaynak | metindeki adaylar):")
    for fis, g, C, m in sessiz[:10]:
        print(f"   {fis} | {g} | {C['vkn']} | {C['kaynak']} | {vkn_adaylari(m)}")

    sozluk_yanlislari = [(f, g, C, m) for f, g, A, B, C, m in sonuclar
                         if C["kaynak"] == "satici_sozlugu" and C["vkn"] != g]
    if sozluk_yanlislari:
        print(f"\n  Sozlugun YANLIS bildigi {len(sozluk_yanlislari)} fis:")
        for fis, g, C, m in sozluk_yanlislari[:10]:
            print(f"   {fis} | gercek {g} | sozluk {C['vkn']} | {C['detay'].get('sozluk_arama')} | {hata_nedeni(g, m)}")


def csv_yaz(sonuclar):
    """Fis bazinda sonuclari data\\vkn_olcum.csv dosyasina yazar; dosya yolunu dondurur."""
    yol = veri_yolu("vkn_olcum.csv")
    with open(yol, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["fis", "gold_vkn", "A_vkn", "B_vkn", "C_vkn", "C_kaynak", "C_guven", "C_insan_kontrolu", "C_satici", "C_dogru_mu", "C_hata_nedeni", "metindeki_adaylar"])
        for fis, gold, A, B, C, metin in sonuclar:
            dogru = C["vkn"] == gold
            w.writerow([fis, gold, A["vkn"], B["vkn"], C["vkn"], C["kaynak"], C["guven"],
                        C["insan_kontrolu"], C["satici"]["durum"], dogru,
                        "" if dogru else hata_nedeni(gold, metin), " ".join(vkn_adaylari(metin))])
    return yol


def mlflow_kaydet(n, fisler, ozetler, csv_yol):
    """Olcum ozetini MLflow'a yazar ('vkn-olcum' experiment'i). MLflow'a ulasilamazsa olcum yine de biter."""
    try:
        import mlflow
        mlflow.set_tracking_uri(os.getenv("MLFLOW_TRACKING_URI", "http://localhost:5000"))
        mlflow.set_experiment("vkn-olcum")
        with mlflow.start_run(run_name="leave-one-out"):
            mlflow.log_param("olculen_fis", n)
            mlflow.log_param("satici_sayisi", len({v for _, v, _ in fisler}))
            for harf, o in zip("ABC", ozetler):
                mlflow.log_metric(f"{harf}_dogru_orani", o["dogru"] / n)
                mlflow.log_metric(f"{harf}_yanlis_orani", o["yanlis"] / n)
                mlflow.log_metric(f"{harf}_otomatik_kabul_orani", o["oto"] / n)
                mlflow.log_metric(f"{harf}_otomatik_dogruluk", o["oto_dogru"] / o["oto"] if o["oto"] else 0.0)
            mlflow.log_artifact(csv_yol)
        print("MLflow'a kaydedildi: experiment 'vkn-olcum'")
    except Exception as e:
        print(f"MLflow'a kaydedilemedi: {e}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gold", required=True)
    ap.add_argument("--ocr", required=True)
    ap.add_argument("--vlm", default=None)
    ap.add_argument("--mlflow", action="store_true")
    kayma_argumanlari_ekle(ap)
    a = ap.parse_args()

    fisler, sayac = gold_oku(a.gold)
    sonuclar, ocr_yok = fisleri_olc(fisler, a.ocr, a.vlm, kayma_cozucu(a))

    n = len(sonuclar)
    if n == 0:
        print("Olculecek fis bulunamadi. --gold ve --ocr klasorlerindeki dosya adlari ayni olmali ({fis}.json).")
        return

    ozetler = [yontem_ozeti(sonuclar, i) for i in range(3)]
    ozet_yazdir(n, ozetler, sonuclar, sayac, ocr_yok, vlm_verildi=bool(a.vlm))
    hata_analizi_yazdir(sonuclar)

    csv_yol = csv_yaz(sonuclar)
    print(f"\nFis bazinda sonuclar: {os.path.join('data', 'vkn_olcum.csv')}  (hatali olanlari Excel'de filtreleyip incelemek icin)")

    if a.mlflow:
        mlflow_kaydet(n, fisler, ozetler, csv_yol)


if __name__ == "__main__":
    main()
