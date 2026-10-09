"""
FAZ B'nin SON adimi: `onay_durumu` (admin/muhasebe kararinin sonucu) ground-truth
etiketini üretir.

Neden AYRI bir modül ve neden EN SONDA:
  - onay_durumu ancak açiklama METNİ üretildikten SONRA anlamlidir. Faz A'da
    (main.py) atanirsa "önce karar, sonra açiklama" gibi ters bir nedensellik
    kurulur; oysa gerçekte muhasebe önce açiklamayi okur, sonra karar verir.
  - Ollama bu etiketi HİÇBİR ZAMAN görmez. Üretim tarafi (batch_hazirla ->
    aciklama_toplu_uret) yalnizca `aciklama_kategorisi`'ni tasir; `onay_durumu`
    o dosyalarin hiçbirine girmez, burada üretilip AYRI etiket dosyasina yazilir.
    Model girdisi (`aciklama_birlestir.py` -> faturalar_aciklamali.json) da bu
    alani içermez -- ikisini karistirma.

KARAR KURALI (2026-07-28 kararı) -- sürücü: (is_anomali, aciklama_kategorisi)

                  ANOMALİSİZ            ANOMALİLİ (A/B ayrimi YOK)
    yeterli       onaylandi             onaylanmadi
    yetersiz      gozden_gecirilecek    onaylanmadi
    manipulatif   gozden_gecirilecek    onaylanmadi
    ai_uretimi    gozden_gecirilecek    onaylanmadi

Yani: anomali varsa DOĞRUDAN RED (teknik/davranissal ayrimi yapilmaz).
Anomali yoksa yalniz `yeterli` onaylanir; diger üç açiklama kategorisi
incelemeye düser.

CIFT KURALI (2026-08-11) -- yukaridaki tablonun TEK istisnasi. Sira
`yukleme_zamani`'ndan okunur; `cift_grup_id` hiçbir zaman model girdisine girmez.

    mukerrer, once yuklenen   ek anomali var        -> onaylanmadi
    mukerrer, once yuklenen   yalniz mukerrer + yeterli    -> ONAYLANDI
    mukerrer, once yuklenen   yalniz mukerrer + digeri     -> gozden_gecirilecek
    mukerrer, sonra yuklenen  her halde             -> onaylanmadi
    cakisma, iki uye de       ek anomali var        -> onaylanmadi
    cakisma, iki uye de       ek anomali yok        -> gozden_gecirilecek

"once yuklenen + yeterli -> onaylandi" satiri, anomali etiketli bir kaydin onay
aldigi TEK durumdur: unutkanlik senaryosunda ilk yukleme kusursuzdur, suc ikinci
yuklemededir. Cakismada hiç red YOKTUR (ek anomali disinda): numaralandirma
hatasi saticinindir, çalisanin görüs alani disinda.

Önceki kural (A/B grubu × kategori, 12 hücreli tablo) BIRAKILDI: B-grubu teknik
hatalar "muhasebe düzeltir, onaylar" varsayimiyla gozden_gecirilecek'e düsüyordu.
Yeni kuralda anomalinin kaynagi (calisan mi, sistem mi) onay kararini degistirmez;
o ayrim zaten `anomali_turleri` etiketinde duruyor (schema.anomali_grubu ile
istenildigi an türetilebilir).

NOT: bu kuralla anomalili tarafta onay_durumu fiilen is_anomali'nin bir kopyasidir;
bilgi yalnizca ANOMALİSİZ tarafta (kategori ekseninde) ek sinyal tasir.

NEREYE YAZILIR: Faz B sonunda elde iki dosya kalir, `kayit_id` ile eslesirler:

    data/faturalar_aciklamali.json            <- aciklama_birlestir.py  (MODEL GİRDİSİ:
                                                 fatura alanlari + aciklama_metni)
    data/faturalar_aciklamali_etiketler.json  <- BU MODÜL (GROUND TRUTH: is_anomali,
                                                 anomali_turleri, aciklama_kategorisi,
                                                 onay_durumu)

Faz A'nin `data/faturalar_etiketler.json` dosyasi salt GİRDİdir, üzerine yazilmaz;
üretilmis açiklamalar da (`data/aciklama/batch_*_ciktilar.json`) oldugu gibi kalir.

Kullanim:
    python -m faz_b_aciklama_uretimi.onay_durumu_ata                       # varsayilan yollar
    python -m faz_b_aciklama_uretimi.onay_durumu_ata --tum-kayitlar        # açiklamasizlari da yaz (onay_durumu "")
    python -m faz_b_aciklama_uretimi.onay_durumu_ata --cikti-dizini data/aciklama \
        --etiket-json data/faturalar_etiketler.json \
        --output-json data/faturalar_aciklamali_etiketler.json
"""

import argparse
import glob
import json
from collections import Counter
from pathlib import Path

from ortak.cift_grup import cift_grup_id, ciftleri_bul
from faz_b_aciklama_uretimi.iliskisel_es_etiketle import ILISKISEL_TURLER, esleri_etiketle

VARSAYILAN_CIKTI_DIZINI = "data/aciklama"
VARSAYILAN_ETIKET_JSON = "data/faturalar_etiketler.json"
VARSAYILAN_OUTPUT_JSON = "data/faturalar_aciklamali_etiketler.json"
VARSAYILAN_GIRDI_JSON = "data/faturalar_aciklamali.json"

ONAY_DURUMLARI = ["onaylandi", "gozden_gecirilecek", "onaylanmadi"]


def onay_durumu_belirle(is_anomali: bool, aciklama_kategorisi: str) -> str:
    """(is_anomali, aciklama_kategorisi) -> onay_durumu. Saf fonksiyon.

    Tablo için modül docstring'ine bak. Rastgelelik YOKTUR: gözlenebilir bir
    değişkene bağlanmayan rastgelelik saf etiket gürültüsüdür, öğrenilemez ve
    yalnizca ulasilabilir dogruluk tavanini düsürür."""
    if is_anomali:
        return "onaylanmadi"
    return "onaylandi" if aciklama_kategorisi == "yeterli" else "gozden_gecirilecek"


def uretilen_kayitlari_yukle(dizin: Path) -> dict[str, dict]:
    """batch_*_ciktilar.json -> {kayit_id: cikti_kaydi}.

    Anahtar fatura_no DEĞİL kayit_id: mukerrer_fis_yukleme / fatura_no_cakismasi
    anomalilerinde ayni fatura_no iki kayitta bulunur (bkz. CLAUDE.md §14)."""
    kayitlar: dict[str, dict] = {}
    for yol in sorted(glob.glob(str(dizin / "batch_*_ciktilar.json"))):
        with open(yol, "r", encoding="utf-8") as f:
            cikti = json.load(f)
        for kayit_id, kayit in cikti.items():
            kayitlar[kayit_id] = kayit
    return kayitlar


def cift_kararlari(girdi_map: dict[str, dict], kategori_map: dict[str, str],
                   tur_map: dict[str, set]) -> tuple[dict[str, str], int]:
    """Cift uyelerinin onay_durumu'u. Tablo modül docstring'inde.

    Sira `yukleme_zamani`'ndan okunur; alani eksik olan cift ATLANIR (temel
    kurala düser) ve sayisi raporlanir -- sessizce yanlis sira uydurmaktansa."""
    kayitlar = [girdi_map[k] for k in kategori_map if k in girdi_map]
    karar: dict[str, str] = {}
    sirasiz = 0
    for uyeler in ciftleri_bul(kayitlar).values():
        turler = {k: tur_map[k] for k in uyeler}
        ortak = set().union(*turler.values()) & ILISKISEL_TURLER
        if not ortak:
            continue
        if any(not girdi_map[k].get("yukleme_zamani") for k in uyeler):
            sirasiz += 1
            continue
        if "mukerrer_fis_yukleme" in ortak:
            sirali = sorted(uyeler, key=lambda k: girdi_map[k]["yukleme_zamani"])
            once = sirali[0]
            if turler[once] - {"mukerrer_fis_yukleme"}:
                karar[once] = "onaylanmadi"
            elif kategori_map[once] == "yeterli":
                karar[once] = "onaylandi"
            else:
                karar[once] = "gozden_gecirilecek"
            for k in sirali[1:]:
                karar[k] = "onaylanmadi"
        else:
            for k in uyeler:
                karar[k] = ("onaylanmadi" if turler[k] - {"fatura_no_cakismasi"}
                            else "gozden_gecirilecek")
    return karar, sirasiz


def girdiyle_hizala(uretilen: dict[str, dict], girdi_json: str) -> dict[str, dict]:
    """Model girdisinde OLMAYAN kayitlari düser.

    `aciklama_birlestir.py --eleme` bazi kayitlari veri setine sokmuyor; bu modül
    ayni dizini süzgeçsiz okudugu icin onlara yine de etiket üretirdi ve iki dosya
    farkli kayit kümesi tasirdi. Üyeligin TEK kaynagi birlestirmenin ciktisidir;
    kategori yine batch dizininden okunur (girdi dosyasi o alani tasimaz).

    Dosya yoksa hizalama yapilmaz -- birlestirme henüz kosulmamis olabilir."""
    yol = Path(girdi_json)
    if not yol.exists():
        print(f"[!] Girdi dosyasi yok ({girdi_json}); hizalama YAPILMADI. "
              f"aciklama_birlestir.py --eleme ile kosulduysa iki dosyanin kayit "
              f"kümesi FARKLI olur, once onu kosup bu modülü tekrar calistir.")
        return uretilen
    with open(yol, "r", encoding="utf-8") as f:
        girdi_idler = {k["kayit_id"] for k in json.load(f)}
    hizali = {kid: k for kid, k in uretilen.items() if kid in girdi_idler}
    dusen = len(uretilen) - len(hizali)
    if dusen:
        print(f"[+] Girdi dosyasiyla hizalandi: {dusen} kayit düstü "
              f"({len(uretilen)} -> {len(hizali)}), kaynak {girdi_json}")
    return hizali


def etiketleri_uret(etiketler: list[dict], uretilen: dict[str, dict],
                    girdi_map: dict[str, dict],
                    tum_kayitlar: bool = False) -> tuple[list[dict], dict]:
    """Açiklamasi ÜRETİLMİŞ kayitlar için onay_durumu'lu etiket listesi + rapor.

    Açiklamasi olmayan kayitlar ATLANIR: onay_durumu, okunmus bir açiklamanin
    sonucudur; metin yoksa etiket de üretilmez.

    tum_kayitlar=True: etiket dosyasindaki TÜM kayitlar yazilir, açiklamasi
    olmayanlarda onay_durumu BOŞ kalir. Model girdisi `aciklama_birlestir.py`
    --sadece-uretilenler OLMADAN üretildiyse (yani 100k satirin tamami yazildiysa)
    iki dosyanin satir kümesi ayni kalsin diye."""
    sonuc: list[dict] = []
    kategori_uyusmazligi = 0
    is_anomali_uyusmazligi = 0
    aciklamasiz = 0
    dagilim = Counter()
    capraz = Counter()

    # 1. gecis: etkin kategori ve tur kumeleri (cift karari ikisine de muhtac)
    kategori_map: dict[str, str] = {}
    tur_map: dict[str, set] = {}
    for etiket in etiketler:
        kayit = uretilen.get(etiket["kayit_id"])
        if kayit is None:
            continue
        kategori = kayit.get("aciklama_kategorisi") or etiket.get("aciklama_kategorisi", "")
        if kategori != etiket.get("aciklama_kategorisi"):
            kategori_uyusmazligi += 1
        kategori_map[etiket["kayit_id"]] = kategori
        tur_map[etiket["kayit_id"]] = set(etiket.get("anomali_turleri", []))

    cift_karar, cift_sirasiz = cift_kararlari(girdi_map, kategori_map, tur_map)

    # 2. gecis: yazim
    for etiket in etiketler:
        kayit = uretilen.get(etiket["kayit_id"])
        if kayit is None:
            if not tum_kayitlar:
                continue
            # Açiklama yok -> karar da yok. Alan BOŞ birakilir (uydurma etiket
            # üretmektense eksikligi görünür kalsin).
            aciklamasiz += 1
            sonuc.append({
                "kayit_id": etiket["kayit_id"],
                "fatura_no": etiket["fatura_no"],
                # is_kolu + harcama_kategorileri Faz A etiketinden AYNEN taşinir
                # (leakage degil, ground-truth etiket; alan_adlari.py bunlari okur).
                "is_kolu": etiket.get("is_kolu", ""),
                "harcama_kategorileri": etiket.get("harcama_kategorileri", []),
                "cift_grup_id": cift_grup_id(girdi_map[etiket["kayit_id"]])
                if etiket["kayit_id"] in girdi_map else "",
                "is_anomali": bool(etiket.get("is_anomali", bool(etiket.get("anomali_turleri")))),
                "anomali_turleri": etiket.get("anomali_turleri", []),
                "aciklama_kategorisi": etiket.get("aciklama_kategorisi", ""),
                "onay_durumu": "",
            })
            continue

        kid = etiket["kayit_id"]
        # Kategori kaynagi: ÜRETİMDE fiilen kullanilan kategori (batch çiktisi);
        # --kategori-override kullanildiysa etiket dosyasindakinden farki olabilir.
        kategori = kategori_map[kid]
        anomali_turleri = etiket.get("anomali_turleri", [])
        is_anomali = bool(etiket.get("is_anomali", bool(anomali_turleri)))
        if is_anomali != bool(anomali_turleri):
            is_anomali_uyusmazligi += 1

        durum = cift_karar.get(kid) or onay_durumu_belirle(is_anomali, kategori)
        dagilim[durum] += 1
        capraz[("anomalili" if is_anomali else "temiz", kategori, durum)] += 1

        sonuc.append({
            "kayit_id": kid,
            "fatura_no": etiket["fatura_no"],
            # is_kolu + harcama_kategorileri Faz A etiketinden AYNEN taşinir
            # (leakage degil, ground-truth etiket; alan_adlari.py bunlari okur).
            "is_kolu": etiket.get("is_kolu", ""),
            "harcama_kategorileri": etiket.get("harcama_kategorileri", []),
            "cift_grup_id": cift_grup_id(girdi_map[kid]) if kid in girdi_map else "",
            "is_anomali": is_anomali,
            "anomali_turleri": anomali_turleri,
            "aciklama_kategorisi": kategori,
            "onay_durumu": durum,
        })

    kararli = sum(dagilim.values())
    rapor = {
        "uretilen_aciklama_sayisi": len(uretilen),
        "yazilan_kayit_sayisi": len(sonuc),
        "etiketlenen_kayit_sayisi": kararli,          # onay_durumu DOLU olanlar
        "aciklamasiz_kayit_sayisi": aciklamasiz,      # yalniz --tum-kayitlar modunda >0
        "eslesmeyen_aciklama_sayisi": len(uretilen) - kararli,
        "kategori_uyusmazligi": kategori_uyusmazligi,
        "is_anomali_uyusmazligi": is_anomali_uyusmazligi,
        "cift_karari_verilen": len(cift_karar),
        "cift_sirasiz": cift_sirasiz,
        "onay_durumu_dagilimi": dict(dagilim),
        "capraz_dagilim": {f"{a}|{k}|{d}": s for (a, k, d), s in sorted(capraz.items())},
    }
    return sonuc, rapor


def raporu_yazdir(rapor: dict) -> None:
    toplam = rapor["etiketlenen_kayit_sayisi"]
    print(f"[+] {toplam} kayda onay_durumu atandi "
          f"(dosyaya yazilan toplam kayit: {rapor['yazilan_kayit_sayisi']}).")
    if rapor["aciklamasiz_kayit_sayisi"]:
        print(f"[+] {rapor['aciklamasiz_kayit_sayisi']} kayitta açiklama yok "
              f"-> onay_durumu BOŞ birakildi (--tum-kayitlar).")
    if rapor["eslesmeyen_aciklama_sayisi"]:
        print(f"[!] UYARI: {rapor['eslesmeyen_aciklama_sayisi']} üretilmis açiklamanin "
              f"kayit_id'si etiket dosyasinda ESLESMEDİ.")
    if rapor["kategori_uyusmazligi"]:
        print(f"[!] {rapor['kategori_uyusmazligi']} kayitta batch kategorisi ile etiket "
              f"kategorisi farkli (--kategori-override kullanilmis olabilir); "
              f"BATCH kategorisi esas alindi.")
    if rapor["is_anomali_uyusmazligi"]:
        print(f"[!] UYARI: {rapor['is_anomali_uyusmazligi']} kayitta is_anomali ile "
              f"anomali_turleri çelisiyor; is_anomali esas alindi.")
    print(f"[+] {rapor['cift_karari_verilen']} cift uyesine CIFT kurali uygulandi.")
    if rapor["cift_sirasiz"]:
        print(f"[!] UYARI: {rapor['cift_sirasiz']} ciftte yukleme_zamani yok, "
              f"temel kurala dustu (sira uydurulmadi).")

    print("\n[+] onay_durumu dagilimi:")
    for durum in ONAY_DURUMLARI:
        adet = rapor["onay_durumu_dagilimi"].get(durum, 0)
        oran = adet / toplam if toplam else 0.0
        print(f"    {durum:20s} {adet:6d}  (%{oran*100:.1f})")


def main():
    parser = argparse.ArgumentParser(
        description="Üretilen açiklamalara göre onay_durumu ground-truth etiketini ata (Faz B son adim)")
    parser.add_argument("--cikti-dizini", default=VARSAYILAN_CIKTI_DIZINI,
                        help="batch_*_ciktilar.json dizini (üretilmis açiklamalar)")
    parser.add_argument("--etiket-json", default=VARSAYILAN_ETIKET_JSON,
                        help="Faz A etiket dosyasi (is_anomali/anomali_turleri/aciklama_kategorisi)")
    parser.add_argument("--output-json", default=VARSAYILAN_OUTPUT_JSON,
                        help="onay_durumu eklenmis etiket dosyasi (YENİ dosya)")
    parser.add_argument("--tum-kayitlar", action="store_true",
                        help="Açiklamasi olmayan kayitlari da yaz (onay_durumu BOŞ). "
                             "aciklama_birlestir.py --sadece-uretilenler OLMADAN "
                             "koşulduysa iki dosyanin satir kümesi ayni kalsin diye.")
    parser.add_argument("--girdi-json", default=VARSAYILAN_GIRDI_JSON,
                        help="aciklama_birlestir.py ciktisi; etiket kümesi buna "
                             "hizalanir (--eleme kullanildiysa sart). Yoksa uyarilir.")
    parser.add_argument("--rapor-json", default=None,
                        help="opsiyonel: dagilim raporunu bu yola yaz")
    args = parser.parse_args()

    uretilen = uretilen_kayitlari_yukle(Path(args.cikti_dizini))
    print(f"[+] {len(uretilen)} adet üretilmis açiklama bulundu ({args.cikti_dizini}).")
    uretilen = girdiyle_hizala(uretilen, args.girdi_json)
    if not uretilen:
        print("HATA: hiç üretilmis açiklama yok. Önce aciklama_toplu_uret.py çalistir.")
        return

    girdi_map: dict[str, dict] = {}
    if Path(args.girdi_json).exists():
        with open(args.girdi_json, "r", encoding="utf-8") as f:
            girdi_map = {r["kayit_id"]: r for r in json.load(f)}

    with open(args.etiket_json, "r", encoding="utf-8") as f:
        etiketler = json.load(f)
    print(f"[+] {len(etiketler)} etiket okundu ({args.etiket_json}).")

    # Ciftin iki uyesi de iliskisel turu tasimali (etiket ASIMETRIK kalmasin).
    degisen = esleri_etiketle(list(girdi_map.values()),
                              {x["kayit_id"]: x for x in etiketler})
    if degisen:
        print(f"[+] {degisen} ese iliskisel etiket basildi (cift simetrigi).")

    if etiketler and "onay_durumu" in etiketler[0]:
        print("[!] Girdi etiket dosyasinda eski bir onay_durumu alani var (eski kuraldan "
              "kalma); YOK SAYILIP yeniden hesaplaniyor.")

    sonuc, rapor = etiketleri_uret(etiketler, uretilen, girdi_map,
                                   tum_kayitlar=args.tum_kayitlar)
    if not rapor["etiketlenen_kayit_sayisi"]:
        print("HATA: hiçbir üretilmis açiklama etiket dosyasiyla eslesmedi (kayit_id uyumsuz?).")
        return

    with open(args.output_json, "w", encoding="utf-8") as f:
        json.dump(sonuc, f, ensure_ascii=False, indent=2)

    raporu_yazdir(rapor)
    print(f"\n[+] Etiketler -> {args.output_json}")

    if args.rapor_json:
        with open(args.rapor_json, "w", encoding="utf-8") as f:
            json.dump(rapor, f, ensure_ascii=False, indent=2)
        print(f"[+] Rapor    -> {args.rapor_json}")


if __name__ == "__main__":
    main()
