from decimal import Decimal
from datetime import date
from ortak.schema import (
    Fatura, FaturaKalemi, HarcamaKategorisi, IsKolu, KDV_ORANI_MAP, paraya_yuvarla,
    IS_KOLU_KATEGORILERI, POLICY_YASAKLI_KATEGORILER,
)
from ortak.politika import kalem_limiti
from faz_a_fis_uretimi.generators.anomaly_injector import ANOMALI_FONKSIYONLARI
from faz_a_fis_uretimi.generators.field_generator import FIYAT_ARALIGI_GENEL, ANOMALI_URUN_MAKULLUGU




# 1. Kimlik No Doğrulamasi (VKN / TCKN)

def vkn_checksum_dogrula(vkn: str) -> bool:
    """10 haneli VKN'nin checksum algoritmasina uygunluğunu doğrular."""
    if len(vkn) != 10 or not vkn.isdigit():
        return False

    digits = [int(d) for d in vkn[:9]]
    total = 0
    for i in range(9):
        d = (digits[i] + (9 - i)) % 10
        if d != 0:
            d = (d * (2 ** (9 - i))) % 9
            if d == 0:
                d = 9
        total += d

    beklenen_check = (10 - (total % 10)) % 10
    return beklenen_check == int(vkn[9])


def tckn_checksum_dogrula(tckn: str) -> bool:
    """11 haneli TCKN'nin checksum algoritmasina uygunluğunu doğrular."""
    if len(tckn) != 11 or not tckn.isdigit():
        return False
    if tckn[0] == "0":
        return False

    digits = [int(d) for d in tckn[:9]]
    tek_toplam = sum(digits[0:9:2])
    cift_toplam = sum(digits[1:8:2])

    onuncu_beklenen = ((tek_toplam * 7) - cift_toplam) % 10
    on_birinci_beklenen = sum(digits + [onuncu_beklenen]) % 10

    return int(tckn[9]) == onuncu_beklenen and int(tckn[10]) == on_birinci_beklenen


def kimlik_no_dogrula(kimlik_no: str) -> bool:
    """Hane sayisina göre VKN ya da TCKN algoritmasini seçip doğrular."""
    if len(kimlik_no) == 10:
        return vkn_checksum_dogrula(kimlik_no)
    elif len(kimlik_no) == 11:
        return tckn_checksum_dogrula(kimlik_no)
    return False


# 2. Matematiksel Tutarlilik

def kalem_ara_toplam_dogrula(kalem: FaturaKalemi) -> bool:
    """ara_toplam = (miktar * birim_fiyat) * (1 - iskonto/100) doğru mu?"""
    beklenen = (
        Decimal(str(kalem.miktar)) * kalem.birim_fiyat
        * (Decimal("1") - Decimal(str(kalem.iskonto_orani)) / Decimal("100"))
    )
    return abs(kalem.ara_toplam - beklenen) < Decimal("0.01")


def kalem_satir_toplam_dogrula(kalem: FaturaKalemi) -> bool:
    """satir_toplam = ara_toplam + kdv_tutari doğru mu?"""
    beklenen = kalem.ara_toplam + kalem.kdv_tutari
    return abs(kalem.satir_toplam - beklenen) < Decimal("0.01")

def kalem_kdv_tutari_dogrula(kalem: FaturaKalemi) -> bool:
    """kdv_tutari = ara_toplam * kdv_orani/100 doğru mu?"""
    beklenen = paraya_yuvarla(kalem.ara_toplam * Decimal(str(kalem.kdv_orani)) / Decimal("100"))
    return abs(kalem.kdv_tutari - beklenen) < Decimal("0.01")


def fatura_genel_toplam_dogrula(fatura: Fatura) -> bool:
    """genel_toplam = tüm kalemlerin satir_toplam toplami mi?"""
    beklenen = sum((k.satir_toplam for k in fatura.kalemler), Decimal("0"))
    return abs(fatura.genel_toplam - beklenen) < Decimal("0.01")


#3. Fatura No Tekrar Kontrolü (Hepsi unique olmali)

def fatura_no_tekrarlarini_bul(faturalar: list[Fatura]) -> list[tuple[str, str]]:
    """
    Ayni satici_vkn altinda birden fazla kez geçen fatura no'lari döndürür.
    Farkli VKN'li firmalarin ayni fatura no'yu kullanmasi anomali sayilmaz.
    """
    gorulen: dict[tuple[str, str], int] = {}
    for fatura in faturalar:
        anahtar = (fatura.satici_vkn, fatura.fatura_no)
        gorulen[anahtar] = gorulen.get(anahtar, 0) + 1
    return [anahtar for anahtar, sayi in gorulen.items() if sayi > 1]


#4. Tarih Kontrolü (Gelecek tarihli fatura olmamali)
def tarih_gelecekte_mi(fatura_tarihi_str: str, bugun_str: str) -> bool:
    """Fatura tarihi bugünden ileri bir tarihse True döner (hata durumu)."""
    return fatura_tarihi_str > bugun_str  # ISO format (YYYY-MM-DD) string karşilaştirmasi güvenlidir


#5.a Mevzuat değişikliği oabilmesi nedeni ile kategori_kdv_doğrula kaldırıldı. Kategoriye göre KDV oranı kontrolü artık yapılmayacak.

#5b. İş Kolu - Kategori Uyum Kontrolü
def yasakli_kalem_saticiya_makul_mu(kalem: FaturaKalemi, is_kolu: IsKolu | None) -> bool:
    """
    Yasakli kategorideki bir kalemi, bu SATICININ fiilen satip satmadigini
    soyler (politika ihlali olup olmadigini DEGIL -- o ayri eksen, bkz.
    kalem_yasakli_kategoride_mi).

    Kaynak: data/anomali_verileri/anomali_urunler.csv `makul_is_kollari` sutunu
    (bkz. field_generator.anomali_urun_makullugu_yukle docstring'i -- gerekce
    ve olcum orada). Restoranda alkol / markette sigara / akaryakit
    istasyonunda piyango bileti satici acisindan OLAGANDIR, yalnizca sirket
    politikasina aykiridir.

    Yasakli olmayan kalemler icin False doner (bu fonksiyonun konusu degil).
    """
    if is_kolu is None or kalem.harcama_kategorisi not in POLICY_YASAKLI_KATEGORILER:
        return False
    return is_kolu.value in ANOMALI_URUN_MAKULLUGU.get(kalem.aciklama.strip(), set())


def kalem_is_kolu_uyumu_dogrula(
    kalem: FaturaKalemi,
    izinli_kategoriler: list[HarcamaKategorisi],
    is_kolu: IsKolu | None = None,
) -> bool:
    """
    Kalemin kategorisi, faturanın iş koluna izinli kategoriler arasında mı?

    `is_kolu` verilirse ek olarak SATICI MAKULLUGU eksenine bakilir: yasakli
    kategorideki bir kalem, o is kolunun fiilen sattigi bir urunse (restoranda
    alkol, markette sigara) uyumsuzluk SAYILMAZ -- politika ihlali
    `yasakli_kategori` etiketiyle zaten ayrica isaretleniyor. Parametre
    verilmezse eski davranis (her yasakli kalem uyumsuzluk) korunur.
    """
    if kalem.harcama_kategorisi in izinli_kategoriler:
        return True
    return yasakli_kalem_saticiya_makul_mu(kalem, is_kolu)


#5c. Politika İhlali — Yasakli Kategori Kontrolü
def kalem_yasakli_kategoride_mi(kalem: FaturaKalemi) -> bool:
    """Kalem, iş kolu bağlamından bağımsız olarak politika gereği yasaklı bir kategoride mi?"""
    return kalem.harcama_kategorisi in POLICY_YASAKLI_KATEGORILER


#5d. Politika İhlali — Limit Aşımı Kontrolü
def kalem_limit_asimi_mi(kalem: FaturaKalemi) -> bool:
    """Kalemin birim fiyati, kategorisi için tanimli politika limitini aşiyor mu?"""
    limit = kalem_limiti(kalem.harcama_kategorisi, kalem.birim)
    return limit is not None and kalem.birim_fiyat > Decimal(str(limit))

#5e/5f. Ondalık (Decimal-Point) Kayması Tespiti — Fat-Finger Fiyat Hatası
#
# Eski "fahiş fiyat / düşük fiyat" kontrolü statik ve DAR bir fiyat bandı
# kullandığı için enflasyonla eskiyip false-positive üretiyordu ve kaldırılmıştı.
# Burada geri getirilen kontrol farklıdır: amacı enflasyona bağlı DRIFT'i değil,
# ondalık noktasının 10x/100x yanlış yazılmasından (fat-finger) doğan KABA
# kaymayı yakalamaktır (bkz. anomaly_injector.ondalik_kaymasi_anomali_uret /
# dusuk_ondalik_kaymasi_anomali_uret). Bu yüzden band CÖMERT tutulur: normal
# üretimdeki ±%30 aralık-dışı gürültüyü de, limit_asimi'nin ürettiği yüksek ama
# meşru fiyatları da YAKALAMAZ; yalnızca büyüklük sırası (order-of-magnitude)
# sapmalarını ayıklar. Cömert band enflasyona da dayanıklıdır (dar statik bandın
# aksine).
ONDALIK_KAYMASI_BANT_CARPANI = Decimal("5")

# ALT yön için AYRI (daha geniş) çarpan. Sebep: fiyat dağılımı sağa çarpık olduğu
# için `min / 5` eşiği doğal ucuz kalemlerin İÇİNE düşerken `max * 5` doğal
# fiyatların çok üstünde kalıyordu -> union, doğal ucuz kalemleri toplayıp
# dusuk_ondalik_kaymasi'ni şişiriyordu. Ölçüldü (100k): dusuk 1685 vs yukari 1249;
# fazlaliğin tamami (400 fatura) `5x-10x altinda` şeridindeydi, yani asimetri 42x.
# ÷10'a çikinca dusuk ~1285'e iner (asimetri ~1x). Enjekte edilen etiketler
# KAYBOLMAZ (union additive; enjektörün ground truth'u ayrica durur) -- yalnizca
# union'in doğal kuyruğu toplamasi durur.
ONDALIK_KAYMASI_ALT_BANT_CARPANI = Decimal("10")


def kalem_ondalik_kaymasi_yukari_mi(kalem: FaturaKalemi) -> bool:
    """
    Kalemin birim fiyatı, kategorisinin makul bandının (FIYAT_ARALIGI_GENEL üst
    sınırı) ONDALIK_KAYMASI_BANT_CARPANI katını aşıyor mu? Aşıyorsa YUKARI yönlü
    fat-finger ondalık kayması (10x/100x) şüphesi -> 'ondalik_kaymasi' etiketi.

    NOT (bilinçli taviz): dar bantlı kategorilerde küçük tutarlı bir kalemin 10x
    kayması bandı aşmayabilir; o kayma bu kontrolle yakalanamaz. Ground truth
    zaten enjektör tarafında TAM etiketlenir (fatura_no üzerinden), bu kontrol
    union tarafında yalnızca KABA kaymaları geri kazanır.

    Band olağan ±%30 fiyat varyansını elemeyecek kadar cömerttir; yalnızca
    büyüklük-sırası (order-of-magnitude) aykırılıkları tetikler. Nadiren TEMİZ bir
    fatura da tetikleyebilir (geniş bantlı kategorilerde doğal gürültünün uç
    kuyruğu gerçekten aşırı bir fiyat ürettiğinde) -- bu bir yanlış-pozitif DEĞİL,
    union etiketlemenin amacıdır: doğal varyansın ürettiği gerçek büyüklük-sırası
    aykırılığı da etiketlenmelidir (bkz. kural_ihlali_turlerini_tespit_et notu).
    """
    aralik = FIYAT_ARALIGI_GENEL.get(kalem.harcama_kategorisi)
    if aralik is None:
        return False
    high = Decimal(str(aralik[1]))
    return kalem.birim_fiyat > high * ONDALIK_KAYMASI_BANT_CARPANI


def kalem_ondalik_kaymasi_asagi_mi(kalem: FaturaKalemi) -> bool:
    """
    Kalemin birim fiyatı, kategorisinin makul bandının (FIYAT_ARALIGI_GENEL alt
    sınırı) ONDALIK_KAYMASI_ALT_BANT_CARPANI katı ALTINDA mı? Altındaysa AŞAĞI yönlü
    fat-finger ondalık kayması (÷10/÷100) şüphesi -> 'dusuk_ondalik_kaymasi'
    etiketi. Yukarı yöndeki eşdeğerinin (kalem_ondalik_kaymasi_yukari_mi) aynı
    bilinçli tavizi geçerlidir.
    """
    aralik = FIYAT_ARALIGI_GENEL.get(kalem.harcama_kategorisi)
    if aralik is None:
        return False
    low = Decimal(str(aralik[0]))
    return kalem.birim_fiyat < low / ONDALIK_KAYMASI_ALT_BANT_CARPANI


#6. VKN-Firma Tutarlilik Kontrolü (Ayni VKN farkli unvan, ya da ayni unvan farkli VKN)
# Bu anomaliler fatura ÖRNEĞİNİN kimlik alanini mutasyona uğratir (registry'yi
# DEĞİL): gecersiz_kimlik_no satici VKN'sini bilerek bozar. Ayni firmanin diğer
# faturalari GERÇEK VKN'siyle durduğu için, tutarlilik haritasi bunu "ayni ad /
# farkli VKN" çelişkisi sanar -- oysa kasitli bir anomalidir ve kendi etiketi vardir.
# Ölçüldü: 100k'lik koşuda 1208 firma adi bu yüzden çelişkili görünüyor ve
# bunlarin 4560'i HİÇ anomalisi olmayan tertemiz fatura ("komşu" etkisi); rapordaki
# "Hatali Fatura" sayisini gerçek anomali sayisinin ~4560 üstüne çikariyordu.
# Bu faturalar HER İKİ haritadan da muaf tutulur: bozuk VKN tesadüfen başka bir
# firmanin gerçek VKN'siyle çakişirsa asil önemli yönde (ayni_vkn_farkli_ad) da
# sahte alarm doğardi.
KIMLIK_MUAF_ANOMALILER = ("gecersiz_kimlik_no",)


def vkn_firma_tutarlilik_hatalarini_bul(faturalar: list[Fatura]) -> dict:
    """
    Ayni VKN'nin farkli firma adlariyla, ya da ayni firma adinin
    farkli VKN'lerle eşleştiği durumlari tespit eder.

    KIMLIK_MUAF_ANOMALILER etiketli faturalar haritalara HİÇ girmez (bkz. sabitin
    üstündeki not) -- muafiyet O(1): faturanin kendi anomali_turleri'ne bakilir,
    registry taranmaz.
    """
    vkn_to_adlar: dict[str, set[str]] = {}
    ad_to_vknler: dict[str, set[str]] = {}

    for fatura in faturalar:
        turler = getattr(fatura, "anomali_turleri", None) or ()
        if any(t in turler for t in KIMLIK_MUAF_ANOMALILER):
            continue
        vkn_to_adlar.setdefault(fatura.satici_vkn, set()).add(fatura.satici_unvan)
        ad_to_vknler.setdefault(fatura.satici_unvan, set()).add(fatura.satici_vkn)

    celiskili_vkn = {vkn: adlar for vkn, adlar in vkn_to_adlar.items() if len(adlar) > 1}
    celiskili_ad = {ad: vknler for ad, vknler in ad_to_vknler.items() if len(vknler) > 1}

    return {
        "ayni_vkn_farkli_ad": celiskili_vkn,
        "ayni_ad_farkli_vkn": celiskili_ad,
    }
#7. Fatura Tutarlilik Kontrolü (Footer toplamlari ile kalem toplamlari uyumlu mu?)
def fatura_footer_tutarlilik_dogrula(fatura: Fatura) -> bool:
    """toplam_vergisiz_tutar + toplam_kdv_tutari == genel_toplam mi?"""
    beklenen = fatura.toplam_vergisiz_tutar + fatura.toplam_kdv_tutari
    return abs(fatura.genel_toplam - beklenen) < Decimal("0.01")
# İş kolu kategori uyumusuzluğu durumundaki faturaları doğrular




#Şirket politikası bazlı doğrulamar(kategori bazında limit aşımı, tek kalem limit aşımları, vb.) gibi kurallar da buraya eklenebilir.
#Yasaklı kategori veya ürünler için de kontrol fonksiyonları eklenebilir. önemli!!
#Ek olarak ondalıklı sayıyın doğruluğunu kontrol etmek için de fonksiyonlar eklenebilir. 
#Örneğin, birim fiyatın veya miktarın belirli bir hassasiyette olup olmadığını kontrol etmek gibi.
#Hatta, birim fiyata kasıtlı anomali ekleyip daha sonra burada kontrol fonksiyonu ile tespit edebiliriz.
#yukarıdakilerin hepsi eklendi.
def fatura_dogrula(fatura: Fatura, bugun_str: str) -> list[str]:
    """
    Tek bir faturayi tüm kurallara göre kontrol eder.
    Dönüş: ihlal edilen kurallarin isim listesi (boşsa fatura tamamen geçerli demektir).
    """
    hatalar: list[str] = []

    if not kimlik_no_dogrula(fatura.satici_vkn):
        hatalar.append(f"Geçersiz satici kimlik no: {fatura.satici_vkn}")

    if tarih_gelecekte_mi(fatura.fatura_tarihi, bugun_str):
        hatalar.append(f"Gelecek tarihli fatura: {fatura.fatura_tarihi}")

    if not fatura_footer_tutarlilik_dogrula(fatura):
        hatalar.append("Footer toplamlari (vergisiz + KDV) genel toplamla uyuşmuyor")

    if not fatura_genel_toplam_dogrula(fatura):
        hatalar.append("Genel toplam, kalemlerin toplamiyla uyuşmuyor")
    
    izinli_kategoriler = IS_KOLU_KATEGORILERI.get(fatura.is_kolu, [])

    for kalem in fatura.kalemler:
        if not kalem_ara_toplam_dogrula(kalem):
            hatalar.append(f"Kalem {kalem.kalem_no}: ara_toplam hesabi hatali")
        if not kalem_kdv_tutari_dogrula(kalem):
            hatalar.append(f"Kalem {kalem.kalem_no}: kdv_tutari hesabi hatali")
        if not kalem_satir_toplam_dogrula(kalem):
            hatalar.append(f"Kalem {kalem.kalem_no}: satir_toplam hesabi hatali")
        if not kalem_is_kolu_uyumu_dogrula(kalem, izinli_kategoriler, fatura.is_kolu):
            hatalar.append(
                f"IS_KOLU_UYUMSUZLUGU: Kalem {kalem.kalem_no} iş koluna ({fatura.is_kolu.value}) "
                f"uygun değil: {kalem.harcama_kategorisi.value}"
            )
        if kalem_yasakli_kategoride_mi(kalem):
            hatalar.append(
                f"YASAKLI_KATEGORI: Kalem {kalem.kalem_no} yasakli kategoride: "
                f"{kalem.harcama_kategorisi.value}"
            )
        if kalem_limit_asimi_mi(kalem):
            limit = kalem_limiti(kalem.harcama_kategorisi, kalem.birim)
            hatalar.append(
                f"LIMIT_ASIMI: Kalem {kalem.kalem_no} ({kalem.harcama_kategorisi.value}) "
                f"birim fiyati limiti aşiyor: {kalem.birim_fiyat} > {limit}"
            )
        if kalem_ondalik_kaymasi_yukari_mi(kalem):
            hatalar.append(
                f"ONDALIK_KAYMASI: Kalem {kalem.kalem_no} ({kalem.harcama_kategorisi.value}) "
                f"birim fiyati kategori bandini aşiri aşiyor (fat-finger şüphesi): "
                f"{kalem.birim_fiyat}"
            )
        elif kalem_ondalik_kaymasi_asagi_mi(kalem):
            hatalar.append(
                f"DUSUK_ONDALIK_KAYMASI: Kalem {kalem.kalem_no} ({kalem.harcama_kategorisi.value}) "
                f"birim fiyati kategori bandinin aşiri altinda (fat-finger şüphesi): "
                f"{kalem.birim_fiyat}"
            )

    return hatalar

# 7b. Union (Additive) Etiketleme İçin Bağımsız Kural İhlali Tespiti

def kural_ihlali_turlerini_tespit_et(fatura: Fatura) -> set[str]:
    """
    Faturayı üretici (anomaly_injector) tarafında ne enjekte edildiğinden
    BAĞIMSIZ olarak, sadece kural tabanlı kontrollere göre tarar ve ihlal
    edilen kural türlerini ANOMALI_FONKSIYONLARI ile ayni isimlendirmeyle
    döner. Bu, union/additive etiketleme stratejisinin doğrulayıcı
    tarafıdır -- amaç, bir anomali fonksiyonunun yan etkisiyle (ör.
    limit_asimi eklerken kazara is_kolu uyumsuzluğu da oluşması) veya saf
    doğal varyansla (ör. fiyat gürültüsünün tesadüfen limiti aşması)
    tetiklenen ihlalleri, ayrı manuel yama gerektirmeden otomatik
    yakalamaktir.

    ondalik_kaymasi / dusuk_ondalik_kaymasi ARTIK BURADA yakalanır: bu iki
    "fat-finger" anomalisi kalem içi matematiği bozmaz (hesap kontrollerini
    tetiklemez) ama birim fiyatı büyüklük sırası olarak kaydırır; cömert bir
    fiyat-makullüğü bandıyla (kalem_ondalik_kaymasi_yukari_mi /
    kalem_ondalik_kaymasi_asagi_mi) KABA kaymalar geri kazanılır. Dar bantta
    kalan küçük kaymalar yine sadece üretici tarafında
    etiketli kalır (bkz. o fonksiyonun notu).

    KASITLI OLARAK BURADA YOK: basamak_karisikligi, sistematik_yuvarlama --
    bunlar tanımı gereği matematiksel/kural bazlı doğrulamadan kaçan, sadece
    üretici tarafında etiketlenmesi gereken "sinsi" anomalilerdir. Ayrıca
    ayni-fatura-no türleri (mukerrer_fis_yukleme / fatura_no_cakismasi) ve VKN-firma
    tutarlılığı da YOK -- bunlar tek bir faturaya
    değil, faturalar ARASI ilişkiye bakar, bu fonksiyonun kapsamı dışında.
    """
    turler: set[str] = set()
    izinli_kategoriler = IS_KOLU_KATEGORILERI.get(fatura.is_kolu, [])

    for kalem in fatura.kalemler:
        if not kalem_kdv_tutari_dogrula(kalem):
            turler.add("kdv_tutari")
        if not kalem_satir_toplam_dogrula(kalem):
            turler.add("satir_toplami")
        if not kalem_is_kolu_uyumu_dogrula(kalem, izinli_kategoriler, fatura.is_kolu):
            turler.add("is_kolu_kategori_uyumsuzlugu")
        if kalem_yasakli_kategoride_mi(kalem):
            turler.add("yasakli_kategori")
        if kalem_ondalik_kaymasi_yukari_mi(kalem):
            turler.add("ondalik_kaymasi")
        if kalem_ondalik_kaymasi_asagi_mi(kalem):
            turler.add("dusuk_ondalik_kaymasi")
        if kalem_limit_asimi_mi(kalem):
            turler.add("limit_asimi")

    if not fatura_genel_toplam_dogrula(fatura):
        turler.add("genel_toplam")
    if not fatura_footer_tutarlilik_dogrula(fatura):
        turler.add("footer_kismi")

    bugun_str = date.today().isoformat()
    if tarih_gelecekte_mi(fatura.fatura_tarihi, bugun_str):
        turler.add("gelecek_tarihli")
    if not kimlik_no_dogrula(fatura.satici_vkn):
        turler.add("gecersiz_kimlik_no")

    return turler

# 8. Üretim Sonrasi Invariant Kontrolü (regresyon güvenlik ağı)

FATURA_PARA_ALANLARI = ("toplam_vergisiz_tutar", "toplam_kdv_tutari",
                        "toplam_iskonto", "genel_toplam")
KALEM_SAYISAL_ALANLARI = ("miktar", "birim_fiyat", "iskonto_orani", "kdv_orani",
                          "ara_toplam", "kdv_tutari", "satir_toplam")


def _ondalik_haneden_uzun(deger, hane: int = 2) -> bool:
    try:
        ussu = Decimal(str(deger)).as_tuple().exponent
    except Exception:
        return False
    return isinstance(ussu, int) and -ussu > hane


def uzun_ondalik_uyarilari(faturalar: list[Fatura], hane: int = 2) -> list[str]:
    sayac: dict[str, int] = {}
    for f in faturalar:
        for alan in FATURA_PARA_ALANLARI:
            if _ondalik_haneden_uzun(getattr(f, alan, 0), hane):
                sayac[alan] = sayac.get(alan, 0) + 1
        for kalem in f.kalemler:
            for alan in KALEM_SAYISAL_ALANLARI:
                if _ondalik_haneden_uzun(getattr(kalem, alan, 0), hane):
                    sayac[f"kalem.{alan}"] = sayac.get(f"kalem.{alan}", 0) + 1
    return [f"{alan}: {adet} değerde {hane} haneden uzun ondalik var "
            f"(gerçek fişe basilmaz, tür imzasi olur)."
            for alan, adet in sorted(sayac.items())]


def veri_seti_dogrula(faturalar: list[Fatura], hedef_oran: float, tolerans: float = 0.05) -> list[str]:
    """
    Üretilen veri setinin temel beklentilerini kontrol eder, ihlalleri
    string listesi olarak döner (boşsa her şey beklenen sinirlar içinde
    demektir). Bu fonksiyon etiketleri DEĞİŞTİRMEZ, sadece uyarir --
    amaci gelecekte sessizce oluşabilecek regresyonlari (ör. bir anomali
    türünün hiç üretilememesi, oranin ciddi sapmasi) erkenden yakalamak.
    """
    uyarilar: list[str] = []

    if not faturalar:
        return ["Fatura listesi boş"]

    gercek_oran = sum(f.is_anomali for f in faturalar) / len(faturalar)
    if abs(gercek_oran - hedef_oran) > tolerans:
        uyarilar.append(
            f"Anomali orani hedeften sapiyor: gerçek={gercek_oran:.3f}, "
            f"hedef={hedef_oran:.3f}, tolerans={tolerans:.3f}"
        )

    turler_sayaci: dict[str, int] = {}
    for f in faturalar:
        for tur in f.anomali_turleri:
            turler_sayaci[tur] = turler_sayaci.get(tur, 0) + 1

    for isim in ANOMALI_FONKSIYONLARI:
        if turler_sayaci.get(isim, 0) == 0:
            uyarilar.append(f"Anomali türü hiç üretilmemiş: {isim}")

    # Alan alan Fatura kuran enjektörler (AnomaliliFatura) yeni alanlari tasimayi
    # unutabiliyor; alan o zaman sessizce bos kalir (olculdu: yukleme_zamani,
    # 482/12000). Yeni bir zorunlu alan eklendiginde BURAYA da ekle.
    for alan in ("yukleme_zamani", "saat", "adres"):
        bos = sum(1 for f in faturalar if not getattr(f, alan, ""))
        if bos:
            uyarilar.append(
                f"{alan} BOŞ olan {bos} fatura var; bir anomali enjektörü alani "
                f"tasimiyor (AnomaliliFatura kurucularina bak)."
            )

    # Adres VKN'nin aksine hiçbir anomali tarafindan bagimsiz bozulmaz -- ayni
    # unvanin farkli adresi HER ZAMAN bug'dir, gecersiz_kimlik_no gibi bir
    # muafiyete gerek yok.
    ad_to_adresler: dict[str, set[str]] = {}
    for f in faturalar:
        ad_to_adresler.setdefault(f.satici_unvan, set()).add(f.adres)
    celiskili_adres = {ad: adresler for ad, adresler in ad_to_adresler.items() if len(adresler) > 1}
    if celiskili_adres:
        uyarilar.append(f"Ayni firma adi farkli adreslerle eslesmis: {len(celiskili_adres)} unvan.")

    # Iliskisel cift SIMETRIK etiketlenmeli: tur OLAYI isaretler, kayit bazli
    # karari onay_durumu verir. Tek uye etiketliyse enjektorde regresyon var.
    ciftler: dict[tuple, list[Fatura]] = {}
    for f in faturalar:
        ciftler.setdefault((f.satici_vkn, f.fatura_no), []).append(f)
    for tur in ("mukerrer_fis_yukleme", "fatura_no_cakismasi"):
        asimetrik = sum(1 for uyeler in ciftler.values() if len(uyeler) > 1
                        and 0 < sum(tur in f.anomali_turleri for f in uyeler) < len(uyeler))
        if asimetrik:
            uyarilar.append(f"{tur}: {asimetrik} çiftte yalniz BİR üye etiketli "
                            f"(çift simetrik etiketlenmeli).")

    uyarilar.extend(uzun_ondalik_uyarilari(faturalar))

    return uyarilar

def dogrulama_raporu_olustur(faturalar: list[Fatura]) -> dict:
    bugun_str = date.today().isoformat()

    tekrar_eden_kayitlar = set(fatura_no_tekrarlarini_bul(faturalar))   # (vkn, no) çiftleri    
    vkn_firma_hatalari = vkn_firma_tutarlilik_hatalarini_bul(faturalar)
    ayni_vkn_farkli_ad = vkn_firma_hatalari["ayni_vkn_farkli_ad"]
    ayni_ad_farkli_vkn = vkn_firma_hatalari["ayni_ad_farkli_vkn"] 

    fatura_hatalari: dict[int, dict] = {}   # artik indeks bazli önceden fatura no key di tekrari halinde eski fatura nonun üstüne yazilacakti, bu yüzden dict[int, dict] tipinde
    ondalik_kaymasi_detaylari: list[dict] = []        # fat-finger yukarı yönlü kayma
    dusuk_ondalik_kaymasi_detaylari: list[dict] = []  # fat-finger aşağı yönlü kayma
    yasakli_kategori_detaylari: list[dict] = []
    limit_asimi_detaylari: list[dict] = []
    is_kolu_uyumsuzlugu_sayisi = 0

    for i, fatura in enumerate(faturalar):
        hatalar = fatura_dogrula(fatura, bugun_str)
        if (fatura.satici_vkn, fatura.fatura_no) in tekrar_eden_kayitlar:
            hatalar.append(f"Fatura no birden fazla kez kullanilmiş (ayni VKN altinda): {fatura.fatura_no}")
        
        if fatura.satici_vkn in ayni_vkn_farkli_ad:   #aynı VKN birden fazla şirkette varsa geçersizlik sayilir
            hatalar.append(f"Satici VKN'si farkli unvanlarla eşleşmiş: {fatura.satici_vkn}")
        
        if fatura.satici_unvan in ayni_ad_farkli_vkn:   # ayni ada sahip farkli vknler varsa, geçersiz.
           hatalar.append(f"Satici unvani farkli VKN'lerle eşleşmiş: {fatura.satici_unvan}")
            
        if hatalar:
            fatura_hatalari[i] = {"fatura_no": fatura.fatura_no, "hatalar": hatalar}

            for hata in hatalar:
                if hata.startswith("YASAKLI_KATEGORI:"):
                    yasakli_kategori_detaylari.append({"fatura_no": fatura.fatura_no, "detay": hata})
                elif hata.startswith("LIMIT_ASIMI:"):
                    limit_asimi_detaylari.append({"fatura_no": fatura.fatura_no, "detay": hata})
                elif hata.startswith("ONDALIK_KAYMASI:"):
                    ondalik_kaymasi_detaylari.append({"fatura_no": fatura.fatura_no, "detay": hata})
                elif hata.startswith("DUSUK_ONDALIK_KAYMASI:"):
                    dusuk_ondalik_kaymasi_detaylari.append({"fatura_no": fatura.fatura_no, "detay": hata})
                elif hata.startswith("IS_KOLU_UYUMSUZLUGU:"):
                    is_kolu_uyumsuzlugu_sayisi += 1


    return {
        "toplam_fatura": len(faturalar),
        "hatali_fatura_sayisi": len(fatura_hatalari),
        "gecerli_fatura_sayisi": len(faturalar) - len(fatura_hatalari),
        "fatura_no_tekrarlari": [f"{vkn}:{no}" for vkn, no in tekrar_eden_kayitlar],
        "vkn_firma_tutarsizliklari": vkn_firma_hatalari,
        "hata_detaylari": fatura_hatalari,
        "yasakli_kategori_sayisi": len(yasakli_kategori_detaylari),      # yeni
        "yasakli_kategori_detaylari": yasakli_kategori_detaylari,        # yeni
        "limit_asimi_sayisi": len(limit_asimi_detaylari),                # yeni
        "limit_asimi_detaylari": limit_asimi_detaylari,                  # yeni
        "is_kolu_uyumsuzlugu_sayisi": is_kolu_uyumsuzlugu_sayisi,        # yeni
        "ondalik_kaymasi_sayisi": len(ondalik_kaymasi_detaylari),               # fat-finger yukarı
        "ondalik_kaymasi_detaylari": ondalik_kaymasi_detaylari,                 # fat-finger yukarı
        "dusuk_ondalik_kaymasi_sayisi": len(dusuk_ondalik_kaymasi_detaylari),   # fat-finger aşağı
        "dusuk_ondalik_kaymasi_detaylari": dusuk_ondalik_kaymasi_detaylari,     # fat-finger aşağı
    }


def raporu_yazdir(rapor: dict) -> None:
    print("=" * 60)
    print("  DOĞRULAMA RAPORU")
    print("=" * 60)
    print(f"  Toplam Fatura       : {rapor['toplam_fatura']}")
    print(f"  Geçerli Fatura      : {rapor['gecerli_fatura_sayisi']}")
    print(f"  Hatali Fatura       : {rapor['hatali_fatura_sayisi']}")
    # Liste basilmaz (binlerce çift); tam hali rapor JSON'unda.
    print(f"  Tekrar Eden Fatura No: {len(rapor['fatura_no_tekrarlari'])}")

    vkn_firma = rapor.get("vkn_firma_tutarsizliklari", {})
    ayni_vkn_farkli_ad = vkn_firma.get("ayni_vkn_farkli_ad", {})
    ayni_ad_farkli_vkn = vkn_firma.get("ayni_ad_farkli_vkn", {})

    print(f"\n  Ayni VKN/Farkli Ad Sayisi : {len(ayni_vkn_farkli_ad)}")
    print(f"  Ayni Ad/Farkli VKN Sayisi : {len(ayni_ad_farkli_vkn)}")

    if ayni_vkn_farkli_ad:
        print("\n  ⚠️  Ayni VKN farkli firma adi ile eşleşmiş (daha sikinti)")
        for vkn, adlar in list(ayni_vkn_farkli_ad.items())[:5]:
            print(f"    VKN {vkn}: {adlar}")

    if ayni_ad_farkli_vkn:
        print("\n  Ayni firma adi farkli VKN ile üretilmiş (isim havuzu çakişmasi, düşük öncelikli):")
        for ad, vknler in list(ayni_ad_farkli_vkn.items())[:5]:  #ayni ada sahip farkli vknleri yazdir, 5 ten fazlasini yazdirma.
            print(f"    {ad}: {vknler}")

    print(f"\n  Yasakli Kategori İhlali      : {rapor.get('yasakli_kategori_sayisi', 0)}")
    if rapor.get("yasakli_kategori_detaylari"):
        print("  ⚠️  Yasakli kategori örnekleri:")
        for detay in rapor["yasakli_kategori_detaylari"][:5]:
            print(f"    [{detay['fatura_no']}] {detay['detay']}")

    print(f"  Limit Aşimi İhlali           : {rapor.get('limit_asimi_sayisi', 0)}")
    if rapor.get("limit_asimi_detaylari"):
        print("  ⚠️  Limit aşimi örnekleri:")
        for detay in rapor["limit_asimi_detaylari"][:5]:
            print(f"    [{detay['fatura_no']}] {detay['detay']}")
    
    print(f"  Ondalık Kayması (Yüksek)     : {rapor.get('ondalik_kaymasi_sayisi', 0)}")
    if rapor.get("ondalik_kaymasi_detaylari"):
        print("  ⚠️  Ondalık kayması (yukarı) örnekleri:")
        for detay in rapor["ondalik_kaymasi_detaylari"][:5]:
            print(f"    [{detay['fatura_no']}] {detay['detay']}")

    print(f"  Ondalık Kayması (Düşük)      : {rapor.get('dusuk_ondalik_kaymasi_sayisi', 0)}")
    if rapor.get("dusuk_ondalik_kaymasi_detaylari"):
        print("  ⚠️  Ondalık kayması (aşağı) örnekleri:")
        for detay in rapor["dusuk_ondalik_kaymasi_detaylari"][:5]:
            print(f"    [{detay['fatura_no']}] {detay['detay']}")

    print(f"  İş Kolu-Kategori Uyumsuzluğu : {rapor.get('is_kolu_uyumsuzlugu_sayisi', 0)}")
   

    if rapor["hata_detaylari"]:
        print("\n  İlk 5 hatali faturanin detayi:")
        for i, (indeks, detay) in enumerate(rapor["hata_detaylari"].items()):
            if i >= 5:
                break
            print(f"\n    [{indeks}] Fatura No: {detay['fatura_no']}")
            for hata in detay["hatalar"]:
                print(f"      - {hata}")

    print("=" * 60)