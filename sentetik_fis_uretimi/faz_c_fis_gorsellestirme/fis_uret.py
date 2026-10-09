"""Fatura JSON'larindan GÖRSEL fiş (receipt) render'lari üretir.

Veri boru hattinin parçasi DEĞİLDİR; fişin kendisini üreten ayri araçtir
(bkz. docs/arsiv/faz_a_fatura_uretimi.md).

GÖSTERİM ARİTMETİĞİ (2026-08-17, iskonto kaldırıldıktan sonra):

    brut_birim = birim_fiyat x (1 + kdv_orani/100)      KDV DAHİL birim fiyat
    brut_tutar = birim_fiyat x miktar x (1 + kdv/100)   = satir_toplam

Yuvarlama: brüt tutar TEK SEFERDE yuvarlanir (birim fiyati yuvarlayip
miktarla çarpmak 5 kuruşa kadar sapma üretiyordu). Anomalili kalemlerde
sapma BÜYÜK kalir ve görselde tespit edilebilir -- `kalem_gosterimi`
kaydin kendi `satir_toplam`'ini okur, yeniden hesaplamaz.
"""

import argparse
import hashlib
import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, Template
from playwright.sync_api import sync_playwright

from ortak.cift_grup import cift_grup_anahtari, kayit_imzasi


def kimlik_etiketi(kimlik_no: str) -> str:
    """10 haneliyse VKN, 11 haneliyse TCKN etiketi döndürür."""
    return "TCKN" if len(kimlik_no) == 11 else "VKN"


def tutar_formatla(deger) -> str:
    """1234.5 -> '1.234,50'. Türkçe biçim, DAİMA iki ondalik.

    Ham float'i şablona vermek `*1101.0`, `*648.5` gibi tek ondalikli
    tutarlar üretiyordu: gerçek fişte tutar her zaman iki hanelidir ve
    ayraç virgüldür. Tutarsiz ondalik gösterimi ayrica `ondalik_kaymasi`
    anomalisinin görsel imzasini bulandiriyordu."""
    return (
        f"{float(deger):,.2f}"
        .replace(",", "\x00")   # binlik ayraci geçici işaret
        .replace(".", ",")      # ondalik -> virgül
        .replace("\x00", ".")   # binlik -> nokta
    )


def miktar_formatla(birim: str, miktar: float) -> str:
    """Tam sayiysa '3', değilse '3,45' (Türkçe ondalik, gereksiz .0 basmaz).

    KARAR BIRIME DEGIL DEGERE BAKAR (2026-08-01). Eskiden `birim in
    TAM_SAYI_BIRIMLERI` ise `int(miktar)` basiliyordu; birim listesine 'Ay' ve
    'Paket' eklenince bu, ESKI veriyi (2,72 Ay) sessizce '2 Ay' diye basar,
    satir toplami ise hâlâ 2,72 uzerinden hesaplanirdi -> 7.427 fisde sahte
    "hesap tutmuyor" sinyali. Degere bakarak fis, verinin hangi kosudan
    geldiginden BAGIMSIZ olarak kendi icinde tutarli kalir; birim listesi de
    yalniz URETIMI yonetir (bkz. field_generator.rastgele_miktar)."""
    if float(miktar).is_integer():
        return str(int(miktar))
    return f"{miktar:.2f}".replace(".", ",")


def kalem_gosterimi(kalem: dict) -> dict:
    """Kalemin şablonda basilacak alanlarini hesaplar (ham alanlar korunur).

    ANOMALİYİ DÜZELTMEZ: brüt tutar `birim_fiyat`tan, satir toplami ise
    kaydin kendi `satir_toplam`'indan gelir. Enjekte edilmiş bir sapma
    varsa ikisi uyuşmaz ve fişte GÖRÜNÜR -- amaç budur."""
    kdv_carpani = 1 + kalem["kdv_orani"] / 100
    brut_birim = round(kalem["birim_fiyat"] * kdv_carpani, 2)
    brut_tutar = round(kalem["birim_fiyat"] * kalem["miktar"] * kdv_carpani, 2)
    return {
        **kalem,
        "miktar": miktar_formatla(kalem["birim"], kalem["miktar"]),
        "brut_birim": brut_birim,
        "brut_tutar": brut_tutar,
    }


def _kayit_hash(kayit_id: str) -> int:
    """kayit_id -> kararli tamsayi. random DEĞİL: ayni fatura her koşuda ayni
    şabloni/saati almali, yoksa resume ile yeniden üretilen fiş öncekinden
    farkli görünür."""
    return int(hashlib.md5(kayit_id.encode("utf-8")).hexdigest(), 16)


def tarih_tr(iso_tarih: str) -> str:
    """'2026-07-26' -> '26.07.2026'."""
    y, a, g = iso_tarih.split("-")
    return f"{g}.{a}.{y}"


def saat_uret(tohum: str) -> str:
    """Fiş saati (08:00-20:59). Veride saat alani YOK, gerçek fişte var.

    Tohumdan türetilir: hem kararlidir hem de etiketlerle KORELASYONSUZDUR.
    Saati anomaliye/kategoriye bağli üretmek görsele sahte bir sinyal ekler ve
    aşaği akiştaki model onu kisayol olarak öğrenir.

    Tohum kural olarak kayit_id'dir; TEK istisna `saat_tohumlari_kur`un
    eşleştirdiği mükerrer yükleme çiftleridir (bkz. o fonksiyon)."""
    h = _kayit_hash(tohum)
    return f"{8 + h % 13:02d}:{(h // 13) % 60:02d}"


# Imza alanlari cift_grup'ta (fis_uret ve mukerrer.py ayni tanimi paylasir).


def saat_tohumlari_kur(faturalar: list[dict]) -> dict[str, str]:
    """`kayit_id -> saat tohumu` eşlemesi: AYNI fişin iki kez yüklendiği
    (`mukerrer_fis_yukleme`) çiftlerde iki görselin ayni saati basmasini sağlar.

    Sorun: saat `kayit_id`'nin hash'inden türüyordu, f2 ise f1'in `kayit_id`
    HARİÇ birebir kopyasi. Dolayisiyla ayni kâğit iki farkli saatle basiliyordu
    -- oysa saat kâğidin üstünde basilidir, ikinci yükleme onu değiştirmez.
    Model çelişkili kanit görüyordu: yapisal alanlarin tamami ayni ("ayni fiş")
    ama saat farkli ("iki ayri alişveriş"). Ölçüldü: fisler_25k'da eşi de sette
    olan 125 çiftin 30'u bu durumdaydi; şablon_1'e SAAT satiri eklendikten sonra
    (saatsiz olduğu için sorunsuz görünen 33 çift dahil) 125'inin tamami
    bozulacakti -- bu yüzden iki değişiklik birlikte gitmek ZORUNDA.

    GÜVENLİK -- eşleme etiket dosyasindan DEĞİL verinin kendisinden çikar:
    yalnizca `kayit_id` HARİÇ tüm alanlari birebir eşit olan kayitlar ayni
    tohuma bağlanir. Bu test
      * `fatura_no_cakismasi`'ni (ayni vkn+fatura_no ama tarih/kalemler farkli),
      * doğal fatura_no çakişmalarini (CLAUDE.md §7, ~5 adet; her şey farkli)
    yapisal olarak DIŞARIDA birakir. Etiket render katmanina sizmaz ve "yanliş
    fiş eşitlendi" vakasi tanim geregi imkânsizdir: eşitlenen iki kayit zaten
    birebir ayniysa ayni saati basmalari DOĞRU davraniştir.

    ŞABLON ARTIK DOLAYLI OLARAK EŞİTLENİR (2026-08-21): `sablon_sec` kayit_id
    yerine `fatura_no`'nun ŞEKLİNE (FTR önekli mi) bakıyor, ve fatura_no artık
    VERİ ALANI olarak iki formatta üretiliyor (bkz.
    field_generator.rastgele_fatura_no). Mükerrer/çakışma çiftlerinde f2 f1'in
    fatura_no'sunu birebir devraldığı için (bkz. anomaly_injector.py) ikisi aynı
    formatı, dolayısıyla aynı şablonu alır -- bu burada AYRICA kodlanan bir
    "eşitleme" DEĞİL, format-şablon bağlantısının doğal sonucu.

    ESKİ KARAR (2026-08-21'de kaldırıldı): şablon kasıtlı eşitlenmiyordu, çift
    piksel piksel özdeşleşirse mükerrerlik perceptual hash ile trivial hale
    gelir diye (62 çift ölçülmüştü). Artık geçerli değil -- gerekçe: görseller
    şu an modele girmiyor (OCR kapsam dışı, CLAUDE.md açık iş #8) ve gerçek
    dünyada aynı fiziksel belge zaten aynı türde olur (bir mükerrer yükleme iki
    farklı belge TÜRÜNDE olamaz). Bkz. CLAUDE.md "fiş no formatı" kararı.
    """
    gruplar: dict[tuple[str, str], list[dict]] = {}
    for f in faturalar:
        gruplar.setdefault(cift_grup_anahtari(f), []).append(f)

    tohumlar: dict[str, str] = {}
    aday_grup = 0
    for kayitlar in gruplar.values():
        if len(kayitlar) < 2:
            continue
        aday_grup += 1
        # IMZA_DISI alanlarin disindakilerin imzasina göre kümele.
        kumeler: dict[str, list[str]] = {}
        for f in kayitlar:
            imza = kayit_imzasi(f)
            kumeler.setdefault(imza, []).append(f["kayit_id"])
        for kimlikler in kumeler.values():
            if len(kimlikler) > 1:
                # min(): hangi kaydin f1 oldugu veriden okunamaz (etikete
                # bakmadan). Deterministik bir temsilci yeterli -- amac ortak
                # bir saat, belirli bir saat degil.
                tohum = min(kimlikler)
                for kid in kimlikler:
                    tohumlar[kid] = tohum

    # GÜRÜLTÜLÜ UYARI: ayni (vkn, fatura_no) tasiyan gruplar VAR ama hiçbiri
    # özdeş çikmadiysa imza büyük olasilikla fişte BASILMAYAN bir alan yüzünden
    # ayrişiyordur (IMZA_DISI_ALANLAR'a bak). Sessizce geçerse düzeltme
    # uygulanmadan 24k fiş render edilir ve fark edilmez.
    if aday_grup and not tohumlar:
        print(f"  [!] UYARI: ayni (vkn, fatura_no) tasiyan {aday_grup} grup var ama "
              f"hiçbiri özdeş çikmadi -> saat tohumu UYGULANMADI. Girdi dosyasinda "
              f"IMZA_DISI_ALANLAR'a eklenmemiş fazladan bir alan olabilir.")
    return tohumlar


def kdv_gruplari_kur(kalemler: list[dict]) -> list[dict]:
    """KDV oranina göre kirilim (e-arşiv fişindeki KDV tablosu için).

    kdv_tutari JSON'da yok (2026-08-17) -- satir_toplam - (birim_fiyat x miktar)
    olarak türetilir. iskonto hep 0 oldugu için ara_toplam = birim_fiyat x miktar;
    `kdv_tutari` anomalisi satir_toplam'in içine gömülü kaldigi için bu türetme
    sahte degeri de dogru yansitir."""
    gruplar: dict[float, dict] = {}
    for k in kalemler:
        g = gruplar.setdefault(k["kdv_orani"], {"oran": k["kdv_orani"], "kdv": 0.0, "toplam": 0.0})
        g["kdv"] += k["satir_toplam"] - (k["birim_fiyat"] * k["miktar"])
        g["toplam"] += k["satir_toplam"]
    for g in gruplar.values():
        g["kdv"] = round(g["kdv"], 2)
        g["toplam"] = round(g["toplam"], 2)
    return [gruplar[o] for o in sorted(gruplar)]


def baglam_kur(fatura: dict, saat_tohumlari: dict[str, str] | None = None) -> dict:
    """fatura_to_dict çiktisi + şablonlarin ihtiyaç duyduğu türetilmiş alanlar."""
    kayit_id: str = fatura["kayit_id"]
    # Tohum kural olarak kayit_id; eşlemede varsa mükerrer çiftin ortak tohumu.
    tohum = saat_tohumlari.get(kayit_id, kayit_id) if saat_tohumlari else kayit_id
    baglam = dict(fatura)
    baglam["satici_kimlik_etiketi"] = kimlik_etiketi(fatura["satici_vkn"])
    baglam["kalemler"] = [kalem_gosterimi(k) for k in fatura["kalemler"]]
    baglam["fatura_tarihi_tr"] = tarih_tr(fatura["fatura_tarihi"])
    # `saat` Faz A'da uretilir (2026-08-11). GERIYE DONUK YOL: alani tasimayan
    # eski veri (ornegin data/final_veriler) tohum mekanizmasiyla render edilir.
    baglam["saat"] = fatura.get("saat") or saat_uret(tohum)
    baglam["kdv_gruplari"] = kdv_gruplari_kur(fatura["kalemler"])
    return baglam


def sablon_sec(fatura: dict, sablonlar: list[Template]) -> Template:
    """Faturaya şablon atar. Tek şablonla üretilen 25k fişin hepsi ayni
    göründüğü için model fiş tipini değil tek bir düzeni öğreniyordu.

    Seçim `fatura_no`'nun ŞEKLİNE göre (2026-08-21): "FTR" önekli ise e-arşiv
    formatı -> sablonlar[1] (varsayılan `fis_sablon_2.html`), düz sayıysa
    yazarkasa formatı -> sablonlar[0] (varsayılan `fis_sablon_1.html`). Format
    TEK KAYNAKTIR (bkz. field_generator.rastgele_fatura_no) -- burada yeniden
    KARAR verilmez, yalnız OKUNUR. Etiketten (is_anomali/anomali_turleri)
    bağımsız kalır: fiş tipini anomaliye bağlamak görsele sahte sinyal ekler.

    ESKİ DAVRANIŞ (2026-08-21'e kadar): kayit_id hash'inden (bkz. _kayit_hash)
    deterministik ama fatura_no'dan bağımsız seçilirdi. Bu, mükerrer/çakışma
    çiftlerinin ~yarısının farklı şablonda render edilmesine yol açıyordu --
    format artık şablonu belirlediği ve çift üyeleri aynı fatura_no'yu
    taşıdığı için bu iç tutarsızlık artık yapısal olarak oluşamaz."""
    if len(sablonlar) < 2:
        return sablonlar[0]
    return sablonlar[1] if fatura["fatura_no"].startswith("FTR") else sablonlar[0]


BOLMELER = ("egitim", "dogrulama", "test")


def fisleri_uret(faturalar, output_dir: Path, sablonlar, saat_tohumlari, sayfa,
                 yeniden: bool, dosya_adi_esleme: dict[str, str] | None = None) -> tuple[int, int, int]:
    """Bir fatura listesini render eder. Doner: (uretilen, atlanan, hatali).

    dosya_adi_esleme verilirse cikti dosyasi kayit_id yerine eslenen adi
    kullanir (ör. final_veriler_en icin opak hash) -- ayri bir kopyala/
    yeniden-adlandir adimini gereksiz kilar."""
    output_dir.mkdir(parents=True, exist_ok=True)
    uretilen = atlanan = hatali = 0
    for i, fatura in enumerate(faturalar, start=1):
        # Dosya adi varsayilan KAYIT_ID: `fatura_no` tasarim geregi MUKERRER
        # olabilir (CLAUDE.md §7) ve sira numarasi girdi dosyasina gore kayar.
        ad = (dosya_adi_esleme or {}).get(fatura["kayit_id"], fatura["kayit_id"])
        cikti_yolu = output_dir / f"{ad}.png"
        if cikti_yolu.exists() and not yeniden:
            atlanan += 1
            continue
        try:
            sablon = sablon_sec(fatura, sablonlar)
            sayfa.set_content(sablon.render(**baglam_kur(fatura, saat_tohumlari)))
            sayfa.locator(".receipt-container").screenshot(path=str(cikti_yolu))
            uretilen += 1
        except Exception as e:
            # Tek bozuk kayit uzun kosuyu dusurmesin.
            hatali += 1
            print(f"  [X] {fatura['kayit_id']}: {type(e).__name__}: {e}")
        if i % 500 == 0:
            print(f"  {i}/{len(faturalar)} islendi (uretilen {uretilen}, atlanan {atlanan})")
    return uretilen, atlanan, hatali


def main():
    parser = argparse.ArgumentParser(description="JSON faturalardan fiş (receipt) görseli üretir")
    parser.add_argument("--input-json", help="faturalar.json dosya yolu (--bolme-dizini yoksa ZORUNLU)")
    parser.add_argument("--bolme-dizini", default=None,
                        help="veri_bol.py çıktı dizini. Verilirse üç bölme sırayla "
                             "render edilir: <b>_girdi.json -> <b>_gorsel/. Saat "
                             "tohumları üç bölmenin BİRLEŞİMİNDEN hesaplanır.")
    parser.add_argument("--output-dir", default="data/fisler", help="Görsellerin kaydedileceği klasör")
    parser.add_argument("--template", default="faz_c_fis_gorsellestirme/fis_sablon_1.html,faz_c_fis_gorsellestirme/fis_sablon_2.html",
                        help="Jinja2 şablon yolu/yolları (virgülle ayır). Birden fazlaysa her "
                             "faturaya kayit_id'den deterministik olarak biri atanır.")
    parser.add_argument("--limit", type=int, default=None, help="Test için ilk N faturayı işle (varsayılan: hepsi)")
    parser.add_argument("--yeniden", action="store_true",
                        help="Var olan PNG'leri de yeniden üret (varsayılan: atlanır, resume)")
    parser.add_argument("--tohum-json", default=None,
                        help="Saat tohumlarının hesaplanacağı TAM fatura dosyası "
                             "(varsayılan: --input-json). Alt küme render ederken "
                             "tam dosyayı ver, yoksa mükerrer çiftin eşi görünmez ve "
                             "fiş tam koşudakinden FARKLI saat alır.")
    parser.add_argument("--gorsel-cikti-dizini", default=None,
                        help="--bolme-dizini ile birlikte: <b>_gorsel/ klasörlerini "
                             "kaynak yerine bu dizinin altına yazar (ör. final_veriler_en).")
    parser.add_argument("--kayit-id-esleme", default=None,
                        help="{kayit_id: yeni_ad} JSON dosyası -- verilirse PNG adı "
                             "kayit_id yerine bu eşlemeyi kullanır (ör. opak hash).")
    args = parser.parse_args()

    if not args.bolme_dizini and not args.input_json:
        parser.error("--input-json ya da --bolme-dizini ver.")

    def yukle(yol):
        with open(yol, "r", encoding="utf-8") as f:
            return json.load(f)

    # isler: (etiket, faturalar, cikti_dizini)
    if args.bolme_dizini:
        kaynak = Path(args.bolme_dizini)
        gorsel_kok = Path(args.gorsel_cikti_dizini) if args.gorsel_cikti_dizini else kaynak
        isler = [(b, yukle(kaynak / f"{b}_girdi.json"), gorsel_kok / f"{b}_gorsel")
                 for b in BOLMELER]
        tohum_havuzu = [f for _, fs, _ in isler for f in fs]
    else:
        isler = [("", yukle(args.input_json), Path(args.output_dir))]
        tohum_havuzu = isler[0][1]

    # Tohumlar --limit'ten ONCE ve TAM havuzdan: dilim mukerrer ciftin bir uyesini
    # disarda birakirsa saat tam kosudakinden farkli cikar. Bolme kipinde havuz
    # uc bolmenin birlesimidir, yani zaten tam veri seti.
    if args.tohum_json:
        tohum_havuzu = yukle(args.tohum_json)
    if all(f.get("saat") for f in tohum_havuzu):
        saat_tohumlari = {}
        print("Saat verisi faturada VAR; tohum eşlemesi gerekmedi.")
    else:
        saat_tohumlari = saat_tohumlari_kur(tohum_havuzu)
        print(f"Saat tohumu eşlenen kayıt: {len(saat_tohumlari)} "
              f"({len(set(saat_tohumlari.values()))} mükerrer yükleme kümesi)")

    if args.limit:
        isler = [(ad, fs[: args.limit], d) for ad, fs, d in isler]

    yollar = [Path(y.strip()) for y in args.template.split(",") if y.strip()]
    dizinler = sorted({str(y.parent or Path(".")) for y in yollar})
    env = Environment(loader=FileSystemLoader(dizinler))
    env.filters["tutar"] = tutar_formatla
    sablonlar = [env.get_template(y.name) for y in yollar]

    dosya_adi_esleme = yukle(args.kayit_id_esleme) if args.kayit_id_esleme else None

    toplam = sum(len(fs) for _, fs, _ in isler)
    print(f"{toplam} fatura için fiş üretiliyor "
          f"({len(sablonlar)} şablon: {', '.join(y.name for y in yollar)})...")

    uretilen = atlanan = hatali = 0
    with sync_playwright() as p:
        tarayici = p.chromium.launch()
        sayfa = tarayici.new_page()
        for ad, faturalar, dizin in isler:
            if ad:
                print(f"\n--- {ad.upper()}: {len(faturalar)} fatura -> {dizin}/")
            u, a, h = fisleri_uret(faturalar, dizin, sablonlar, saat_tohumlari,
                                   sayfa, args.yeniden, dosya_adi_esleme)
            uretilen, atlanan, hatali = uretilen + u, atlanan + a, hatali + h
        tarayici.close()

    hedefler = ", ".join(str(d) for _, _, d in isler)
    print(f"\nTamamlandi: {uretilen} fis -> {hedefler}")
    if atlanan:
        print(f"  {atlanan} fis zaten vardi, atlandi (--yeniden ile zorla).")
    if hatali:
        print(f"  [!] {hatali} fis üretilemedi.")


if __name__ == "__main__":
    main()
