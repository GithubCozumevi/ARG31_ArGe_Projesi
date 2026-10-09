import random
from datetime import date, timedelta
from decimal import Decimal

from ortak.schema import (
    Fatura, FaturaKalemi, HarcamaKategorisi,
    IS_KOLU_KATEGORILERI, KDV_ORANI_MAP,
    POLICY_YASAKLI_KATEGORILER, paraya_yuvarla
)
from ortak.politika import kalem_limiti, limitli_kategoriler, zorunlu_kalem_limiti
from faz_a_fis_uretimi.generators.field_generator import (
    ACIKLAMA_HAVUZU, rastgele_birim, birim_sec, rastgele_miktar, rastgele_birim_fiyat,rastgele_fatura,
    fatura_no_kaydini_sifirla,
    mutfak_anahtari,
    firma_kisit_anahtari,
    mukerrer_yukleme_zamanlari,
)



# 1. Gelecek Tarihli Fatura

def gelecek_tarihli_anomali_uret(fatura: Fatura) -> Fatura:
    """Fatura tarihini kasitli olarak gelecekteki bir tarihe çeker."""
    gelecek_tarih = (date.today() + timedelta(days=random.randint(30, 365))).isoformat()
    fatura.fatura_tarihi = gelecek_tarih
    return fatura



# 2. Geçersiz Kimlik No

def gecersiz_kimlik_no_anomali_uret(fatura: Fatura) -> Fatura:
    """Satici kimlik numarasinin SON hanesini (checksum hanesi) bozar.

    Eskiden tüm numara rastgele üretiliyordu; rastgele bir sayinin checksum'a
    tesadüfen UYMA olasiligi ~1/10 olduğu için üretilen faturalarin %9.4'ünde
    'gecersiz_kimlik_no' etiketi vardi ama kimlik numarasi GEÇERLİYDİ -- yani
    etiket var, tespit edilebilir sinyal yok: öğrenilemez saf etiket gürültüsü
    (100k koşusunda 2106 etiketin 198'i).

    Yöntem: son hane (checksum hanesi) için 10 adayin hepsi denenir, checksum'a
    UYMAYANLAR arasindan rastgele biri seçilir. Böylece geçersizlik, gelen
    numaranin başlangiçta geçerli olup olmadigindan BAĞIMSIZ olarak garanti edilir
    (yalnizca "son haneyi kaydir" deseydik, girdi zaten bozuksa yeni hane tesadüfen
    doğru checksum'a denk gelebilirdi). Checksum algoritmasi burada tekrar
    YAZILMAZ; kimlik_no_dogrula hem VKN (10 hane) hem TCKN (11 hane) ayrimini
    kendisi yapar.

    Ayrica tek haneli bozulma, gerçek bir veri giriş hatasina (typo) tüm numaranin
    rastgele değişmesinden daha yakindir.

    NOT: import FONKSİYON İÇİNDE -- validators zaten bu modülden
    ANOMALI_FONKSIYONLARI'ni import ettiği için modül seviyesinde dairesel olurdu."""
    from ortak.validators import kimlik_no_dogrula

    kimlik = fatura.satici_vkn
    govde = kimlik[:-1]
    adaylar = [str(d) for d in range(10) if not kimlik_no_dogrula(govde + str(d))]
    if adaylar:
        fatura.satici_vkn = govde + random.choice(adaylar)
    else:
        # Teorik olarak ulaşilamaz (10 adayin en az 9'u geçersizdir); yine de
        # sessizce yanliş etiket üretmemek için ek bir haneyi de bozalim.
        fatura.satici_vkn = govde[:-1] + str((int(govde[-1]) + 1) % 10) + kimlik[-1]
    return fatura



# 3. KDV-Kategori Uyumsuzluğu Mevzuat değişimden ötürü kaldırılması uygun görüldü.

# def kdv_kategori_uyumsuzlugu_anomali_uret(fatura: Fatura) -> Fatura:
#     """Rastgele bir kalemin KDV oranini, kategorisi için doğru olmayan bir oranla değiştirir."""
#     kalem = random.choice(fatura.kalemler)
#     dogru_oran = KDV_ORANI_MAP[kalem.harcama_kategorisi]
#     olasi_yanlis_oranlar = [o for o in {1.0, 10.0, 20.0} if o != dogru_oran]
#     kalem.kdv_orani = random.choice(olasi_yanlis_oranlar)
#     return fatura



# 4. İş Kolu - Kategori Uyumsuzluğu

def is_kolu_kategori_uyumsuzlugu_anomali_uret(fatura: Fatura) -> Fatura:
    """Faturaya, faturanin is_kolu'suna hiç izinli olmayan bir kalem ekler."""
    izinli_kategoriler = set(IS_KOLU_KATEGORILERI.get(fatura.is_kolu, []))
    tum_kategoriler = set(HarcamaKategorisi)
    yabanci_kategoriler = list(tum_kategoriler - izinli_kategoriler - POLICY_YASAKLI_KATEGORILER)
    yabanci_kategori = random.choice(yabanci_kategoriler)

    yeni_kalem_no = len(fatura.kalemler) + 1
    # Birim ACIKLAMADAN turetilir (birim_sec), rastgele DEGIL: enjekte edilen
    # kalem de fise normal satir olarak basilir ve orada '0,58 Litre Toz Seker'
    # gorunurdu. Enjektorun isi anomali eklemek; birim-urun uyumsuzlugu KASITLI
    # bir anomali degil, istenmeyen gercek disilik.
    aciklama = random.choice(ACIKLAMA_HAVUZU[yabanci_kategori])
    birim = birim_sec(yabanci_kategori, aciklama)
    yeni_kalem = FaturaKalemi(
        kalem_no=yeni_kalem_no,
        aciklama=aciklama,
        harcama_kategorisi=yabanci_kategori,
        miktar=rastgele_miktar(birim, yabanci_kategori, aciklama),
        birim=birim,
        birim_fiyat=rastgele_birim_fiyat(yabanci_kategori, birim),
        iskonto_orani=0.0,
        kdv_orani=KDV_ORANI_MAP[yabanci_kategori],
    )
    fatura.kalemler.append(yeni_kalem)
    return fatura



# 5. Politika İhlali — Yasakli Kategori

def yasakli_kategori_anomali_uret(fatura: Fatura) -> Fatura:
    """Faturaya, kategori ne olursa olsun her zaman reddedilen bir kalem ekler."""
    yasakli_kategori = random.choice(list(POLICY_YASAKLI_KATEGORILER))

    yeni_kalem_no = len(fatura.kalemler) + 1
    aciklama = random.choice(ACIKLAMA_HAVUZU[yasakli_kategori])
    birim = birim_sec(yasakli_kategori, aciklama)
    yeni_kalem = FaturaKalemi(
        kalem_no=yeni_kalem_no,
        aciklama=aciklama,
        harcama_kategorisi=yasakli_kategori,
        miktar=rastgele_miktar(birim, yasakli_kategori, aciklama),
        birim=birim,
        birim_fiyat=rastgele_birim_fiyat(yasakli_kategori, birim),
        iskonto_orani=0.0,
        kdv_orani=KDV_ORANI_MAP[yasakli_kategori],
    )
    fatura.kalemler.append(yeni_kalem)
    return fatura



# 6. Politika İhlali — Limit Aşimi

def limit_asimi_anomali_uret(fatura: Fatura) -> Fatura:
    """Limitli bir kategorideki kalemin birim fiyatini, limitin üstüne kasitli olarak çeker."""
    # (kalem, limit) ikilisi: limit ayrica aranmasin ve None ihtimali burada elensin.
    limitli_kalemler: list[tuple[FaturaKalemi, float]] = []
    for k in fatura.kalemler:
        k_limit = kalem_limiti(k.harcama_kategorisi, k.birim)
        if k_limit is not None:
            limitli_kalemler.append((k, k_limit))

    if not limitli_kalemler:
        # Faturada limitli kategori yok -- rastgele bir limitli kategoriden
        # YENİ bir kalem ekleyip onu limit üstü fiyatlandiriyoruz.
        # Aday: limiti OLAN ve ürün havuzu DOLU kategoriler. Politika dosyasi hiç
        # limit tanimlamamiş olabilir; o durumda fatura değişmeden döner (no-op ->
        # karisik_veri_seti_uret etiket de atamaz), üretim patlamaz.
        adaylar = [k for k in limitli_kategoriler() if ACIKLAMA_HAVUZU.get(k)]
        if not adaylar:
            return fatura
        limitli_kategori = random.choice(adaylar)
        yeni_kalem_no = len(fatura.kalemler) + 1
        aciklama = random.choice(ACIKLAMA_HAVUZU[limitli_kategori])
        birim = birim_sec(limitli_kategori, aciklama)
        limit = zorunlu_kalem_limiti(limitli_kategori, birim)
        yeni_kalem = FaturaKalemi(
            kalem_no=yeni_kalem_no,
            aciklama=aciklama,
            harcama_kategorisi=limitli_kategori,
            miktar=rastgele_miktar(birim, limitli_kategori, aciklama),
            birim=birim,
            birim_fiyat=Decimal(str(round(limit * random.uniform(1.5, 3.0), 2))),
            iskonto_orani=0.0,
            kdv_orani=KDV_ORANI_MAP[limitli_kategori],
        )
        fatura.kalemler.append(yeni_kalem)

        if limitli_kategori not in IS_KOLU_KATEGORILERI.get(fatura.is_kolu, []):
            # Eklenen kategori, iş koluna zaten yabanci -- validator bunu
            # ayrica IS_KOLU_UYUMSUZLUGU olarak da yakalayacak, ground truth
            # bunu yansitsin.
            fatura.anomali_turleri = fatura.anomali_turleri + ["is_kolu_kategori_uyumsuzlugu"]

        return fatura

    kalem, limit = random.choice(limitli_kalemler)
    kalem.birim_fiyat = Decimal(str(round(limit * random.uniform(1.5, 3.0), 2)))
    return fatura



# 7. Fatura No Tekrari (İki Fatura Arasi — Farkli İmza, Ayri Kategori)

def fatura_no_tekrari_anomali_uret(fatura: Fatura, diger_fatura_no: str) -> Fatura:
    """
    Faturanin no'sunu, başka (var olan) bir faturanin no'suyla kasitli
    olarak çakiştirir. DİKKAT: Gerçek bir anomali üretmesi için bu
    fonksiyon, ayni satici_vkn'e sahip iki fatura arasinda kullanilmali —
    farkli VKN'li faturalarin ayni no'yu paylaşmasi artik anomali
    sayilmiyor (validators.py, fatura_no_tekrarlarini_bul).
    """
    fatura.fatura_no = diger_fatura_no
    return fatura


from ortak.schema import AnomaliliFaturaKalemi, AnomaliliFatura

def satir_toplami_anomali_uret(fatura: Fatura) -> Fatura:
    """
    Rastgele bir kalemin satir_toplam'ini kasitli olarak saptirir.
    model_dump() yerine alanlar doğrudan geçirilir; böylece kalem zaten
    başka bir anomaliyle (ör. sahte_ara_toplam) işaretlenmişse o bilgi
    kaybolmaz.
    """
    hedef_index = random.randrange(len(fatura.kalemler))
    orijinal_kalem = fatura.kalemler[hedef_index]

    gercek_toplam = orijinal_kalem.satir_toplam
    carpan = random.choice([
        random.uniform(1.2, 2.0),   # gerçekten fazla gösterilmiş
        random.uniform(0.3, 0.7),   # gerçekten az gösterilmiş
    ])
    sahte_toplam = Decimal(str(round(float(gercek_toplam) * carpan, 2)))

    anomalili_kalem = AnomaliliFaturaKalemi(
        kalem_no=orijinal_kalem.kalem_no,
        aciklama=orijinal_kalem.aciklama,
        harcama_kategorisi=orijinal_kalem.harcama_kategorisi,
        miktar=orijinal_kalem.miktar,
        birim=orijinal_kalem.birim,
        birim_fiyat=orijinal_kalem.birim_fiyat,
        iskonto_orani=orijinal_kalem.iskonto_orani,
        kdv_orani=orijinal_kalem.kdv_orani,
        sahte_ara_toplam=getattr(orijinal_kalem, "sahte_ara_toplam", None),
        sahte_satir_toplam=sahte_toplam,
    )
    fatura.kalemler[hedef_index] = anomalili_kalem
    return fatura


def genel_toplam_anomali_uret(fatura: Fatura) -> Fatura:
    """
    Faturanin genel_toplam'ini kasitli olarak saptirir. kalemler listesi
    dump edilmeden doğrudan geçirilir; bu sayede kalemlerdeki mevcut
    anomaliler (AnomaliliFaturaKalemi tipleri) korunur.
    """
    gercek_toplam = fatura.genel_toplam
    carpan = random.choice([random.uniform(1.1, 1.5), random.uniform(0.5, 0.9)])
    sahte_toplam = Decimal(str(round(float(gercek_toplam) * carpan, 2)))

    return AnomaliliFatura(
        fatura_no=fatura.fatura_no,
        fatura_tarihi=fatura.fatura_tarihi,
        yukleme_zamani=fatura.yukleme_zamani,
        saat=fatura.saat,
        satici_vkn=fatura.satici_vkn,
        satici_unvan=fatura.satici_unvan,
        adres=fatura.adres,
        is_kolu=fatura.is_kolu,   # yeni
        kalemler=fatura.kalemler,
        sahte_toplam_vergisiz_tutar=getattr(fatura, "sahte_toplam_vergisiz_tutar", None),
        sahte_toplam_kdv_tutari=getattr(fatura, "sahte_toplam_kdv_tutari", None),
        sahte_genel_toplam=sahte_toplam,
    )


def footer_kismi_anomali_uret(fatura: Fatura) -> Fatura:
    """
    toplam_kdv_tutari'ni kasitli olarak saptirir; genel_toplam kalemlerin
    gerçek toplamindan hesaplanmaya devam eder. Eskiden toplam_vergisiz_tutar'i
    da hedefleyebiliyordu ama o alan artik disa aktarilmiyor (2026-08-17) --
    o dal secilirse anomali görünmez kaliyordu.
    """
    carpan = random.choice([random.uniform(1.1, 1.4), random.uniform(0.6, 0.9)])

    mevcut_sahte_genel_toplam = getattr(fatura, "sahte_genel_toplam", None)
    mevcut_sahte_vergisiz = getattr(fatura, "sahte_toplam_vergisiz_tutar", None)

    gercek = fatura.toplam_kdv_tutari
    sahte_kdv = Decimal(str(round(float(gercek) * carpan, 2)))
    sahte_vergisiz = mevcut_sahte_vergisiz

    return AnomaliliFatura(
        fatura_no=fatura.fatura_no,
        fatura_tarihi=fatura.fatura_tarihi,
        yukleme_zamani=fatura.yukleme_zamani,
        saat=fatura.saat,
        satici_vkn=fatura.satici_vkn,
        satici_unvan=fatura.satici_unvan,
        adres=fatura.adres,
        kalemler=fatura.kalemler,
        is_kolu=fatura.is_kolu,   # yeni
        sahte_genel_toplam=mevcut_sahte_genel_toplam,
        sahte_toplam_vergisiz_tutar=sahte_vergisiz,
        sahte_toplam_kdv_tutari=sahte_kdv,
    )

def kdv_tutari_anomali_uret(fatura: Fatura) -> Fatura:
    """
    Rastgele bir kalemin kdv_tutari'ni, ara_toplam*kdv_orani/100 formülünden
    kasitli olarak saptirir. kdv_orani DOĞRU kalir (kategoriyle uyumlu) —
    yani kategori_kdv_dogrula geçer ama kalem_kdv_tutari_dogrula patlar.
    satir_toplam (fake edilmediği sürece) bu sahte kdv_tutari üzerinden
    tutarli hesaplanir.
    """
    hedef_index = random.randrange(len(fatura.kalemler))
    orijinal_kalem = fatura.kalemler[hedef_index]

    gercek_kdv_tutari = orijinal_kalem.kdv_tutari
    carpan = random.choice([
        random.uniform(1.3, 2.5),   # olmasi gerekenden çok fazla KDV
        random.uniform(0.1, 0.6),   # olmasi gerekenden az KDV (kaçirma şüphesi)
    ])
    sahte_kdv_tutari = Decimal(str(round(float(gercek_kdv_tutari) * carpan, 2)))

    anomalili_kalem = AnomaliliFaturaKalemi(
        kalem_no=orijinal_kalem.kalem_no,
        aciklama=orijinal_kalem.aciklama,
        harcama_kategorisi=orijinal_kalem.harcama_kategorisi,
        miktar=orijinal_kalem.miktar,
        birim=orijinal_kalem.birim,
        birim_fiyat=orijinal_kalem.birim_fiyat,
        iskonto_orani=orijinal_kalem.iskonto_orani,
        kdv_orani=orijinal_kalem.kdv_orani,
        sahte_ara_toplam=getattr(orijinal_kalem, "sahte_ara_toplam", None),
        sahte_kdv_tutari=sahte_kdv_tutari,
        sahte_satir_toplam=getattr(orijinal_kalem, "sahte_satir_toplam", None),
    )
    fatura.kalemler[hedef_index] = anomalili_kalem
    return fatura


#Sistematik yuvarlama anomalisi kaldırıldı. Gerçek fişlerde kuruş bazlı yuvarlama zaten olabiliyor. False-positive riski.


# 8. Ondalık (Decimal-Point) Kayması — Fat-Finger Veri Girişi Hatası
#
# Bu iki anomali diğer matematiksel anomalilerden FARKLIDIR: sahte_* alanları
# KULLANMAZ, GERÇEK birim_fiyat'ı değiştirir. Dolayısıyla ara_toplam/kdv_tutari/
# satir_toplam kalemin kendi içinde matematiksel olarak TUTARLI kalır -->
# validators.py'deki hesap kontrollerinin hiçbirini tetiklemezler. Tespitleri
# matematiksel değil, FİYAT-MAKULLÜĞÜ (plausibility) temellidir: bir kalem,
# kategorisinin makul fiyat bandını cömert bir katsayı kadar aşarsa/altında
# kalırsa ondalık kayması şüphesi doğar (bkz. validators.kalem_ondalik_kaymasi_yukari_mi
# / kalem_ondalik_kaymasi_asagi_mi).
#
# ADLANDIRMA NOTU: eskiden bu anomali "fahis_fiyat/dusuk_fiyat" adıyla tespit
# edilirdi; o ad ENFLASYONA bağlı fiyat artışı çağrışımı yapıyordu. Oysa bu bir
# VERİ GİRİŞ HATASIdır (ondalık noktasının 10x/100x yanlış yazılması), enflasyon
# değil. Bu yüzden hem enjeksiyon hem tespit artık "ondalik_kaymasi" (yukarı) ve
# "dusuk_ondalik_kaymasi" (aşağı) adlarını kullanır.

# Ondalik kayma büyüklüğü AĞIRLIKLI seçilir (eskiden 10x/100x eşit olasilikliydi).
# Gerçek fat-finger'da ondalik noktasi çoğunlukla TEK basamak kayar (1250.00 ->
# 125.00); iki basamaklik kayma (1250.00 -> 12.50) daha nadirdir. 70/30 bunu
# yansitir ve pozitif sinifin dağilimini gerçek veri giriş hatalarina yaklaştirir.
_KAYMA_CARPANLARI = (Decimal("10"), Decimal("100"))
_KAYMA_AGIRLIKLARI = (0.7, 0.3)


def _kayma_carpani_sec() -> Decimal:
    return random.choices(_KAYMA_CARPANLARI, weights=_KAYMA_AGIRLIKLARI, k=1)[0]


def ondalik_kaymasi_anomali_uret(fatura: Fatura) -> Fatura:
    """
    Fat-finger simülasyonu: rastgele bir kalemin birim_fiyat'ını 10x ya da 100x
    YUKARI kaydırır (ondalık noktası sağa kaymış / lira-kuruş karışıklığı gibi).
    GERÇEK birim_fiyat değiştiği için kalem içi hesaplar (ara_toplam/kdv_tutari/
    satir_toplam) tutarlı kalır; tespit matematiksel doğrulamayla değil,
    fiyat-makullüğü kontrolüyle yapılır (validators.kalem_ondalik_kaymasi_yukari_mi).
    """
    hedef_index = random.randrange(len(fatura.kalemler))
    kalem = fatura.kalemler[hedef_index]

    kayma_carpani = _kayma_carpani_sec()
    kalem.birim_fiyat = paraya_yuvarla(kalem.birim_fiyat * kayma_carpani)

    return fatura


def dusuk_ondalik_kaymasi_anomali_uret(fatura: Fatura) -> Fatura:
    """
    ondalik_kaymasi_anomali_uret'in ters yönü: rastgele bir kalemin birim_fiyat'ı
    10x ya da 100x AŞAĞI kaydırılır (ör. ondalık noktasının sola yazılması,
    kuruş/lira karışıklığı). Yine GERÇEK birim_fiyat değişir, kalemin kendi içi
    hesapları tutarlı kalır; tespit fiyat-makullüğü kontrolüyle yapılır.
    """
    hedef_index = random.randrange(len(fatura.kalemler))
    kalem = fatura.kalemler[hedef_index]

    kayma_carpani = _kayma_carpani_sec()
    kalem.birim_fiyat = paraya_yuvarla(kalem.birim_fiyat / kayma_carpani)

    return fatura


# 9. Basamak/Rakam Karişikliği (Transposition Hatasi) kaldırıldı. OCR bu hatanın ayrımını yapamayabilir güven oranı düşük bir anomali olduğundan kaldırıldı.



ANOMALI_FONKSIYONLARI = {
    "gelecek_tarihli": gelecek_tarihli_anomali_uret,
    "gecersiz_kimlik_no": gecersiz_kimlik_no_anomali_uret,
    #"kdv_kategori_uyumsuzlugu": kdv_kategori_uyumsuzlugu_anomali_uret,
    "is_kolu_kategori_uyumsuzlugu": is_kolu_kategori_uyumsuzlugu_anomali_uret,
    "yasakli_kategori": yasakli_kategori_anomali_uret,
    "limit_asimi": limit_asimi_anomali_uret,
    "kdv_tutari": kdv_tutari_anomali_uret,
    "satir_toplami": satir_toplami_anomali_uret,

    # Fat-finger ondalık kayması (gerçek birim_fiyat 10x/100x kayar; kalem içi
    # hesap tutarlı kalır, tespit fiyat-makullüğü bandıyla -- bkz. validators).
    "ondalik_kaymasi": ondalik_kaymasi_anomali_uret,
    "dusuk_ondalik_kaymasi": dusuk_ondalik_kaymasi_anomali_uret,

    "genel_toplam": genel_toplam_anomali_uret,
    "footer_kismi": footer_kismi_anomali_uret,
    # mukerrer_fis_yukleme / fatura_no_cakismasi burada YOK: iki fatura ARASINDA
    # çalişiyorlar, fatura_no_tekrari_uygula() ile ayri ele aliniyorlar
}


# fatura_no çakişmasinda f2, f1'in HEADER kimlik alanlarini (vkn/unvan/is_kolu)
# aynen devraldiği için, o alanlara bagli anomali etiketleri de senkronlanmali.
# Yalnizca header kapsamli türler listelenir; kalem kapsamli türler f2'ye AİT DEĞİLDİR
# (f2 kendi kalemlerini korur).
HEADER_KAPSAMLI_ANOMALILER = ("gecersiz_kimlik_no",)

# AYNI fatura no'ya sahip iki kayit İKİ FARKLI OLAYDAN doğabilir; ikisi de simüle
# edilir çünkü model ayrimi öğrenmeli:
#   1) mukerrer_fis_yukleme -- çalişan ayni fişi ikinci kez yüklüyor (BİLİNÇLİ
#      kurnazlik, A grubu). Kayitlar yapisal olarak birebir ayni.
#   2) fatura_no_cakismasi  -- satici iki FARKLI fişi ayni no ile kesmiş (satici
#      numaralandirma hatasi, B grubu / teknik). Yalnizca header ortak.
# Payi 1. senaryo lehine tutuyoruz: modelin yakalamasi istenen asil davraniş odur;
# 2. senaryo zaten kalabalik olan teknik anomali sinifina bir ekleme.
MUKERRER_YUKLEME_PAYI = 0.60


def fatura_no_tekrari_uygula(faturalar: list[Fatura], tekrar_sayisi: int) -> None:
    """
    Ayni fatura_no'ya sahip `tekrar_sayisi` kadar çift üretir. DAĞITICIDIR: her çift
    için MUKERRER_YUKLEME_PAYI olasilikla _mukerrer_fis_yukleme_uygula (S1, tam kopya,
    A grubu), aksi halde _fatura_no_cakismasi_uygula (S2, yalniz header ortak, B grubu)
    çağrilir. Ayni fatura no'ya sahip iki kayit iki FARKLI olaydan doğar; ikisi de
    simüle edilir ki model ayrimi öğrenebilsin (bkz. MUKERRER_YUKLEME_PAYI). Her çift icin VKN'nin yani sira SATICI UNVANI da eşitlenir --
    aksi halde ayni VKN farkli unvanla eşleşmiş olur ki bu, kasitli
    ayni-fatura-no anomalisiyle karişan, istenmeyen ayri bir VKN-firma
    tutarsizligi üretir. Gerçek hayatta bir VKN her zaman tek bir firmaya
    ait olduğu icin unvan da eşitlenerek ilişki gerçekçi kaliyor.
    Ayrica ayni faturanin birden fazla çiftte kullanilmasini önlemek icin
    kullanilmiş indeksler takip edilir.

    ÇİFT AYNI is_kolu'ndan seçilir: aynı VKN = aynı satıcı = aynı sektör olduğu
    için (registry mimarisiyle is_kolu artık export edilen bir firma özniteliği),
    f2 f1'in VKN+unvan+is_kolu'sunu TUTARLI biçimde devralir. Kalemler de aynı
    is_kolu'ndan olduğundan istenmeyen bir is_kolu_kategori_uyumsuzlugu yan etiketi
    ÜREMEZ; değişmez korunur (bir VKN -> tek is_kolu).
    """
    if len(faturalar) < 2:
        return

    kullanilmis_indeksler: set[int] = set()

    for _ in range(tekrar_sayisi):
        # Uygun indeksleri (is_kolu, mutfak) ikilisine göre grupla; çift yalnız aynı
        # gruptan seçilir. Mutfak da anahtara dahil çünkü f2 f1'in UNVANINI devralıp
        # kendi KALEMLERİNİ koruyor: pastane adı devralan bir faturada kebap kalemi
        # kalırsa firma adı ile kalemler çelişir (mutfak kısıtının delindiği tek yol).
        gruplar: dict = {}
        for i in range(len(faturalar)):
            if i not in kullanilmis_indeksler:
                f = faturalar[i]
                mutfak = (mutfak_anahtari(f.satici_unvan)
                          if HarcamaKategorisi.YEMEK_HIZMETI in IS_KOLU_KATEGORILERI[f.is_kolu]
                          else None)
                # Restoran DIŞI iş kollarında da aynı gerekçe geçerli (2026-07-30):
                # f2 f1'in UNVANINI devralıp kendi KALEMLERİNİ koruyor, o yüzden çift
                # aynı FIRMA_ADI_KISITLARI grubundan seçilmeli. Aksi halde 'Kırtasiye'
                # adını devralan bir faturada ofis masası kalemi kalır ve kısıt
                # sessizce delinir -- mutfak anahtarıyla kapatılan boşluğun aynısı.
                kisit = firma_kisit_anahtari(f.is_kolu, f.satici_unvan)
                gruplar.setdefault((f.is_kolu, mutfak, kisit), []).append(i)

        uygun_gruplar = [idxler for idxler in gruplar.values() if len(idxler) >= 2]
        if not uygun_gruplar:
            break   # aynı is_kolu'ndan çift kalmadı, daha fazla üretilemez

        idx1, idx2 = random.sample(random.choice(uygun_gruplar), 2)
        f1, f2 = faturalar[idx1], faturalar[idx2]

        if random.random() < MUKERRER_YUKLEME_PAYI:
            faturalar[idx2] = _mukerrer_fis_yukleme_uygula(f1, f2)
        else:
            faturalar[idx2] = _fatura_no_cakismasi_uygula(f1, f2)

        kullanilmis_indeksler.add(idx1)
        kullanilmis_indeksler.add(idx2)


def _mukerrer_fis_yukleme_uygula(f1: Fatura, f2: Fatura) -> Fatura:
    """SENARYO 1 -- çalişan AYNI fişi ikinci kez yüklüyor (bilinçli kurnazlik, A grubu).

    f2, f1'in TAM kopyasi olur: header (unvan/VKN/is_kolu) + TARİH + KALEMLER +
    toplamlar. Yani iki kayit yapisal olarak birebir aynidir; tespit gerçek
    sistemlerdeki gibi (vkn, fatura_no, tutar, kalemler) eşleşmesiyle yapilir.

    Etiketler de f1'inkilerle ayni olur -- f2'nin eski kalemleri (ve onlara bagli
    etiketleri) artik yok, üzerlerine yazildi.

    NOT: aciklama_kategorisi ve aciklama_metni PAYLAŞILMAZ. Ayni fişi iki kez
    yükleyen çalişan ikinci seferde sifirdan bir not yazar; birebir ayni cümleyi
    beklemek gerçekçi olmaz. Bu ayni zamanda veri setini zorlaştirir: yapisal
    alanlar birebir ayni, metin farkli -> model mükerrerliği METİNDEN değil
    yapidan öğrenmek zorunda kalir."""
    # SIRA ONEMLI: kopya f1 ETIKETLENMEDEN alinir, yoksa turu iki kez tasir.
    kopya = f1.model_copy(deep=True)
    kopya.is_anomali = True
    kopya.anomali_turleri = list(f1.anomali_turleri) + ["mukerrer_fis_yukleme"]

    # Cift SIMETRIK etiketlenir: `anomali_turleri` OLAYI isaretler ("bu kayit bir
    # mukerrerlik olayinin parcasi"), kayit bazli karari `onay_durumu` verir
    # (once yuklenen onaylanabilir, sonra yuklenen asla). Tek uyeyi etiketlemek
    # neredeyse ozdes iki kayda zit etiket vermek demekti.
    f1.is_anomali = True
    if "mukerrer_fis_yukleme" not in f1.anomali_turleri:
        f1.anomali_turleri = list(f1.anomali_turleri) + ["mukerrer_fis_yukleme"]

    # Kopya daha GEC yuklenir; iki an tek tabandan simetrik turetilir.
    f1.yukleme_zamani, kopya.yukleme_zamani = mukerrer_yukleme_zamanlari(
        f1.fatura_tarihi, f1.yukleme_zamani)
    return kopya


def _fatura_no_cakismasi_uygula(f1: Fatura, f2: Fatura) -> Fatura:
    """SENARYO 2 -- satici iki FARKLI fişi ayni no ile kesmiş (numaralandirma hatasi,
    B grubu / teknik).

    f2 yalnizca HEADER'i devralir (unvan + VKN + is_kolu); tarih, kalemler, satir ve
    genel toplamlar KENDİSİNİN kalir. Çalişanin görüş alani dişinda bir satici
    hatasidir, dolayisiyla davranişsal değil teknik bir anomalidir.

    İki kayit farkli kalemler taşidiği için açiklama kategorileri de metinleri de
    BAĞIMSIZDIR (her biri kendi anomali profiline göre atanir)."""
    f2.satici_vkn = f1.satici_vkn      # anomalinin gerçek sayilmasi için şart
    f2.satici_unvan = f1.satici_unvan  # ayni VKN = ayni firma tutarliliğini korumak için şart
    f2.adres = f1.adres
    f2.is_kolu = f1.is_kolu            # aynı is_kolu (zaten eşit) -- değişmezi açıkça garanti et
    f2 = fatura_no_tekrari_anomali_uret(f2, f1.fatura_no)
    f2.is_anomali = True
    # HEADER-KAPSAMLI anomali etiketleri f1 ile SENKRONLANIR (tek yönlü devralma
    # DEĞİL): f2 yukarida f1'in kimlik alanlarini AYNEN kopyaladiği için, bu
    # alanlara bagli etiketlerin de f1'inkiyle birebir ayni olmasi gerekir.
    #   - f1 bozuksa  -> f2 de bozuk VKN taşir, etiketi EKLENİR. (Aksi halde f2
    #     etiketsiz bozuk VKN'yle dolaşir; VKN-firma muafiyetine
    #     (validators.KIMLIK_MUAF_ANOMALILER) giremediği için sahte "ayni ad /
    #     farkli VKN" çelişkisi üretiyordu -- 100k'da 1 vaka: 'Burcu Avm'.)
    #   - f1 geçerliyse -> f2'nin kendi bozuk VKN'si ÜZERİNE YAZILDI, artik geçerli;
    #     etiketi KALDIRILIR. (20k'da 4 vaka: etiket var, sinyal yok.)
    # KALEM-kapsamli etiketler (yasakli_kategori, satir_toplami, ondalik_kaymasi...)
    # BİLEREK dokunulmaz -- f2 kendi kalemlerini korur, o etiketler ona aittir.
    turler = [t for t in f2.anomali_turleri if t not in HEADER_KAPSAMLI_ANOMALILER]
    turler += [t for t in HEADER_KAPSAMLI_ANOMALILER if t in f1.anomali_turleri]
    f2.anomali_turleri = turler + ["fatura_no_cakismasi"]

    # Cift SIMETRIK etiketlenir (bkz. _mukerrer_fis_yukleme_uygula). Cakismada
    # iki fis de ayni numaralandirma hatasinin parcasidir; suc saticinindir,
    # dolayisiyla `onay_durumu` ikisine de red DEGIL gozden_gecirilecek verir.
    f1.is_anomali = True
    if "fatura_no_cakismasi" not in f1.anomali_turleri:
        f1.anomali_turleri = list(f1.anomali_turleri) + ["fatura_no_cakismasi"]
    # `yukleme_zamani`'na DOKUNULMAZ: iki farkli fis, iki bagimsiz yukleme.
    return f2

def karisik_veri_seti_uret(adet: int, anomali_orani: float) -> list[Fatura]:
    """
    `adet` kadar fatura üretir, bunlarin `anomali_orani` kadarina rastgele
    bir anomali uygulayip etiketler (is_anomali, anomali_turleri). Etiket
    bilgisi sadece Pydantic modelinde tutulur; JSON export'a (fatura_to_dict)
    dahil edilmez — model bu alanlari GÖRMEMELİ, sadece değerlendirme
    (ground truth) amaçli kullanilmali.
    """
    fatura_no_kaydini_sifirla()   # VKN-kapsamli cakisma-onleme kaydini bu kosu icin sifirla
    faturalar = [rastgele_fatura() for _ in range(adet)]

    anomali_sayisi = int(adet * anomali_orani)
    anomalili_indexler = random.sample(range(adet), min(anomali_sayisi, adet))

    fonksiyon_havuzu = list(ANOMALI_FONKSIYONLARI.items())

    for idx in anomalili_indexler:
        isim, fonksiyon = random.choice(fonksiyon_havuzu)
        oncesi = faturalar[idx].model_copy(deep=True)
        fatura = fonksiyon(faturalar[idx])

        if fatura == oncesi:
            # no-op durumu (ör. tek kalemli faturada sistematik_yuvarlama,
            # ya da basamak_karisikligi'nde erken return) -- anomali
            # gerçekte uygulanmadi, is_anomali/anomali_turleri etiketlenmez.
            faturalar[idx] = fatura
            continue

        fatura.is_anomali = True
        fatura.anomali_turleri = fatura.anomali_turleri + [isim]
        faturalar[idx] = fatura

    fatura_no_tekrar_orani = 0.01
    tekrar_sayisi = int(adet * fatura_no_tekrar_orani)   # 10.000 faturada ~100 çift

    if anomali_orani > 0 and tekrar_sayisi > 0:
        fatura_no_tekrari_uygula(faturalar, tekrar_sayisi)

    return faturalar

if __name__ == "__main__":
    from faz_a_fis_uretimi.generators.field_generator import rastgele_fatura
    from ortak.validators import (
        kalem_ara_toplam_dogrula, kalem_kdv_tutari_dogrula,
        kalem_satir_toplam_dogrula, fatura_footer_tutarlilik_dogrula,
        kalem_ondalik_kaymasi_yukari_mi, kalem_ondalik_kaymasi_asagi_mi,
    )

    def _ondalik_etiket(k):
        if kalem_ondalik_kaymasi_yukari_mi(k):
            return "ondalik_kaymasi"
        if kalem_ondalik_kaymasi_asagi_mi(k):
            return "dusuk_ondalik_kaymasi"
        return "-"

    testler = [
        ("kdv_tutari", kdv_tutari_anomali_uret),
        ("satir_toplami", satir_toplami_anomali_uret),
        # Ondalık kaymaları matematiği BOZMAZ (tüm hesap ✓ kalır); tespitleri
        # fiyat-makullüğü sütununda görünür (ondalik: dolu, diğerlerinde None).
        ("ondalik_kaymasi", ondalik_kaymasi_anomali_uret),
        ("dusuk_ondalik_kaymasi", dusuk_ondalik_kaymasi_anomali_uret),
        ("genel_toplam", genel_toplam_anomali_uret),
        ("footer_kismi", footer_kismi_anomali_uret),
    ]

    for isim, fonksiyon in testler:
        fatura = rastgele_fatura()
        fatura = fonksiyon(fatura)

        print(f"\n--- {isim} ---")
        for k in fatura.kalemler:
            print(f"  ara_toplam:{'✓' if kalem_ara_toplam_dogrula(k) else '✗'} "
                  f"kdv_tutari:{'✓' if kalem_kdv_tutari_dogrula(k) else '✗'} "
                  f"satir_toplam:{'✓' if kalem_satir_toplam_dogrula(k) else '✗'} "
                  f"ondalik:{_ondalik_etiket(k)}")
        print(f"  footer:{'✓' if fatura_footer_tutarlilik_dogrula(fatura) else '✗'}")