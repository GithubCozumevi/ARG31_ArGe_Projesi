from pydantic import BaseModel, Field, field_validator
from decimal import ROUND_HALF_UP, Decimal
from typing import List
from enum import Enum


def paraya_yuvarla(deger: Decimal) -> Decimal:
        """Herhangi bir Decimal değeri 2 ondalik basamağa (kuruş hassasiyeti) yuvarlar."""
        return deger.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

class HarcamaKategorisi(str, Enum):
    YEMEK_HIZMETI = "yemek_hizmeti"      # restoran, catering, iş yemeği
    TEMEL_GIDA = "temel_gida"            # market, bakliyat, süt, et
    ULASIM_HIZMETI = "ulasim_hizmeti"      # B2B: nakliye, kargo, taşimacilik, depolama
    ULASIM_BIREYSEL = "ulasim_bireysel"    # taksi, otobüs/metro, yakit, araç kiralama
    KONAKLAMA = "konaklama"
    OFIS_SARF_MALZEME = "ofis_sarf_malzeme"   # kirtasiye, toner, kağit
    OFIS_MOBILYA = "ofis_mobilya"              # masa, sandalye, kahve makinesi
    TEKNOLOJI_EKIPMAN = "teknoloji_ekipman"    # yazici, monitör, bilgisayar           # bilgisayar, yazici, masa, sandalye, klima
    YAZILIM_LISANS = "yazilim_lisans"
    DANISMANLIK = "danismanlik"
    ALKOL = "alkol"
    EGLENCE = "eglence"
    TUTUN_URUNLERI = "tutun_urunleri"   # yeni
    KUMAR = "kumar"                     # yeni
    DIGER = "diger"                     # ölü kod
    # GIYIM KALDIRILDI (2026-08-14): kurumsal masraf fişinde karşılığı yok
    # (30 gerçek fişte hiç görülmedi) ve tek kaynağı temiz_urunler.csv'nin
    # okunması güç Trendyol adlarıydı. Personel üniforması gerekirse küratörlü
    # kısa bir liste yazılır, B2C havuzu geri getirilmez.
    KISISEL_BAKIM = "kisisel_bakim"      # şampuan, krem, makyaj -- MARKET satar
    TEMIZLIK = "temizlik"                # yeni

# Genel geçer, şirketten bağimsiz yasakli kategoriler (kanunen kabul edilmeyen gider mantiği)
POLICY_YASAKLI_KATEGORILER: set[HarcamaKategorisi] = {
    HarcamaKategorisi.ALKOL,
    HarcamaKategorisi.EGLENCE,
    HarcamaKategorisi.TUTUN_URUNLERI,
    HarcamaKategorisi.KUMAR,
}

# ---------------------------------------------------------------------------
# ANOMALİ GRUPLARI (A/B) -- Tornés/Thornton kalibrasyonu (bkz. simüle_edilmek_istenen.md).
# Bu ayrim dökümantasyondaki düzyaziyi ÇALIŞTIRILABİLİR bir kurala çevirir.
#
#   A) SOSYAL/DAVRANIŞSAL -- çalişan bilinçlidir, manipülasyon ihtimali yüksektir.
#   B) YAPISAL/TEKNİK -- çalişanin görüş alani DIŞINDA (OCR/sistem/hesap hatasi).
#
# DİKKAT: `onay_durumu` artik bu ayrima BAKMAZ (anomali varsa dogrudan red,
# bkz. onay_durumu_ata.py). Ayrim yine de burada duruyor: `anomali_turleri`
# etiketinden her an türetilebilen bir ANALİZ/kirilim ekseni (rapor, alt küme
# seçimi) ve `ACIKLAMA_KATEGORI_ORANLARI` kalibrasyonunun düşünsel temeli.
#
# Not: gelecek_tarihli A grubundadir (çalişan ileri tarihli fiş yükleyemez, sistem
# ya da niyet sorunudur) ama manipulatif ÜSLUP dal seçiminde kullanilmaz --
# orada örtülecek bir kurnazlik yoktur (bkz. aciklama_uretim_core.prompt_olustur).
A_GRUBU_ANOMALILER: set[str] = {
    "yasakli_kategori",
    "is_kolu_kategori_uyumsuzlugu",
    "limit_asimi",
    # Ayni fişi ikinci kez yüklemek BİLİNÇLİ bir kurnazliktir -> A.
    # Kardeşi `fatura_no_cakismasi` (satici iki farkli fişi ayni no ile kesmiş)
    # BİLEREK burada YOK: o çalişanin görüş alani dişinda, teknik bir satici
    # hatasidir -> B grubuna (tümleyene) düşer.
    "mukerrer_fis_yukleme",
    "gelecek_tarihli",
}

# B grubu ENUMERE EDİLMEZ, A'nin tümleyeni olarak tanimlanir: yeni bir teknik
# anomali türü eklendiğinde (ör. yeni bir hesap kontrolü) otomatik B sayilir ve
# yanlişlikla "davranişsal" muamelesi görmez -- güvenli varsayilan budur.
def anomali_grubu(anomali_turleri) -> str:
    """Faturanin anomali grubunu döner: 'temiz' | 'A' | 'B'."""
    if not anomali_turleri:
        return "temiz"
    return "A" if any(t in A_GRUBU_ANOMALILER for t in anomali_turleri) else "B"


# Kategori bazli üst tutar limitleri: `data/politika_limitleri.json`, okuyucusu politika.py.

KDV_ORANI_MAP = {
    HarcamaKategorisi.YEMEK_HIZMETI: 10.0,
    HarcamaKategorisi.TEMEL_GIDA: 1.0,
    HarcamaKategorisi.ULASIM_HIZMETI: 20.0,
    HarcamaKategorisi.ULASIM_BIREYSEL: 20.0,
    HarcamaKategorisi.KONAKLAMA: 10.0,
    HarcamaKategorisi.OFIS_SARF_MALZEME: 20.0,
    HarcamaKategorisi.OFIS_MOBILYA: 20.0,
    HarcamaKategorisi.TEKNOLOJI_EKIPMAN: 20.0,
    HarcamaKategorisi.YAZILIM_LISANS: 20.0,
    HarcamaKategorisi.DANISMANLIK: 20.0,
    HarcamaKategorisi.ALKOL: 20.0,
    HarcamaKategorisi.TUTUN_URUNLERI: 20.0,
    HarcamaKategorisi.KUMAR: 20.0,
    HarcamaKategorisi.EGLENCE: 20.0,
    HarcamaKategorisi.DIGER: 20.0,
    HarcamaKategorisi.KISISEL_BAKIM: 20.0,   # yeni — varsayım, teyit gerekebilir
    HarcamaKategorisi.TEMIZLIK: 20.0,        # yeni — varsayım
}

class IsKolu(str, Enum):
    RESTORAN = "restoran"
    MARKET = "market"
    OTEL = "otel"
    OFIS_TEDARIK = "ofis_tedarik"
    TEKNOLOJI = "teknoloji"
    DANISMANLIK_FIRMASI = "danismanlik_firmasi"
    LOJISTIK_FIRMASI = "lojistik_firmasi"       # eskiden ULASIM_FIRMASI ise adini netleştirdik
    ULASIM_SAGLAYICI = "ulasim_saglayici"        # taksi duraği, akaryakit istasyonu, rent-a-car
    ORGANIZASYON = "organizasyon"
    # GIYIM_MAGAZASI ve KISISEL_BAKIM iş kolları KALDIRILDI (2026-08-14).
    # Ölçüldü: ikisi faturaların %23,1'ini üretiyordu, buna karşılık kişisel
    # bakım firmalarının %76'sı kuaför/berber/güzellik salonu, yani HİZMET
    # satıcısıydı ve havuzda tek hizmet kalemi yoktu; giyimde 195 çelişen
    # satıcı (iç giyim, çocuk) + 11 lüks marka vardı. Kurumsal masraf fişinde
    # ikisinin de karşılığı yok (gerçek fiş etiketlemesi: yemek, yol, otopark).
    # Kişisel bakım ÜRÜNLERİ markette satılmaya devam ediyor.


IS_KOLU_KATEGORILERI = {
    IsKolu.RESTORAN: [HarcamaKategorisi.YEMEK_HIZMETI],
    IsKolu.OTEL: [HarcamaKategorisi.KONAKLAMA, HarcamaKategorisi.YEMEK_HIZMETI],
    IsKolu.OFIS_TEDARIK: [
        HarcamaKategorisi.OFIS_SARF_MALZEME,
        HarcamaKategorisi.OFIS_MOBILYA,
        HarcamaKategorisi.TEKNOLOJI_EKIPMAN,
    ],
    IsKolu.TEKNOLOJI: [
        HarcamaKategorisi.YAZILIM_LISANS,
        HarcamaKategorisi.TEKNOLOJI_EKIPMAN,
    ],
    # Kişisel bakım (şampuan, krem, makyaj) 2026-08-14'te buraya taşındı:
    # market rafında gerçekten var, ayrı bir satıcı iş koluna gerek yok.
    IsKolu.MARKET: [HarcamaKategorisi.TEMEL_GIDA, HarcamaKategorisi.TEMIZLIK,
                    HarcamaKategorisi.KISISEL_BAKIM],
    IsKolu.DANISMANLIK_FIRMASI: [HarcamaKategorisi.DANISMANLIK],
    IsKolu.LOJISTIK_FIRMASI: [HarcamaKategorisi.ULASIM_HIZMETI],
    IsKolu.ULASIM_SAGLAYICI: [HarcamaKategorisi.ULASIM_BIREYSEL],
    IsKolu.ORGANIZASYON: [HarcamaKategorisi.YEMEK_HIZMETI],
}


class FirmaTuru(str, Enum):
    KISA_UNVAN = "kisa_unvan"
    UZUN_UNVAN = "uzun_unvan"
    SAHIS_SIRKETI = "sahis_sirketi"
    YABANCI_ORTAKLI = "yabanci_ortakli"


class FaturaKalemi(BaseModel):
    kalem_no: int
    aciklama: str
    harcama_kategorisi: HarcamaKategorisi
    miktar: float
    birim: str  # "Adet", "Saat", "Kg", "Ay"
    birim_fiyat: Decimal
    iskonto_orani: float = 0.0
    kdv_orani: float  # 1.0, 10.0, 20.0

    

    
    
    @property
    def ara_toplam(self) -> Decimal:
        brut = Decimal(str(self.miktar)) * self.birim_fiyat
        iskonto = brut * Decimal(str(self.iskonto_orani)) / 100
        return paraya_yuvarla(brut - iskonto)

    @property
    def kdv_tutari(self) -> Decimal:
        return paraya_yuvarla(self.ara_toplam * Decimal(str(self.kdv_orani)) / 100)

    @property
    def satir_toplam(self) -> Decimal:
        return paraya_yuvarla(self.ara_toplam + self.kdv_tutari)


class Fatura(BaseModel):
    # Satir bazli BENZERSIZ anahtar. fatura_no benzersiz DEĞİLDİR (mukerrer_fis_yukleme
    # ve fatura_no_cakismasi anomalileri kasitli olarak ayni no'yu iki kayda verir), bu
    # yüzden açiklama boru hatti (batch_hazirla -> toplu_uret -> birlestir) fatura_no
    # yerine BUNUNLA anahtarlanir. Aksi halde çiftin yalnizca biri açiklama üretir ve o
    # tek metin iki kayda birden yazilirdi -- S2'de kalemler farkli olduğu için yanliş,
    # S1'de ise gerçekçi değil (ayni fişi ikinci kez yükleyen çalişan sifirdan not yazar).
    # Her satirda FARKLI olduğu için modele bir sinyal taşimaz; yine de eğitimde
    # özellik olarak KULLANILMAMALI (salt join anahtari).
    kayit_id: str = ""
    fatura_no: str
    fatura_tarihi: str
    # Fisin sisteme yuklendigi an (ISO 8601, +03:00). MODEL GIRDISIDIR, leakage
    # DEGIL: gercek bir masraf uygulamasi yukleme anini bilir. `gelecek_tarihli`
    # enjeksiyonu fatura_tarihi'ni SONRADAN ileri attigi icin o kayitlarda
    # yukleme < fatura tarihi cikar; anomalinin tanimi zaten budur.
    yukleme_zamani: str = ""
    # Fisin uzerinde BASILI saat (HH:MM). Faz A'da uretilir, `fis_uret` yalnizca
    # okur. Uretimde dogmasinin sebebi: mukerrer kopya `model_copy` ile alindigi
    # icin ayni saati DEVRALIR -- render tarafindaki imza/tohum esleme mekanizmasi
    # (fis_uret.saat_tohumlari_kur) gereksizlesir.
    saat: str = ""
    satici_vkn: str  # 10 haneli
    satici_unvan: str
    adres: str = ""
    # Sahis sirketinde ISLETME ADI ile SAHSIN ADI fiste AYRI IKI SATIRDIR
    # (gercek fis: "SAYLA MANTI" / "FEYYAZ ESEN", "FIORE ART PIZZA" / "OSMAN TETIK").
    # Tuzel kisilerde BOS. Fiste basili oldugu icin model girdisidir (OCR okur),
    # ama anomali sinyali tasimaz ve `satici_vkn`in hane sayisiyla %100 korele
    # oldugu icin kimlik ozelligi olarak KULLANILMAMALI.
    satici_sahis_adi: str = ""
    # Alici (bizim sirketimiz) alanlari KALDIRILDI: her faturada ayni sabit
    # degerdi, gercek fiste de yer almiyor.
    kalemler: List[FaturaKalemi]
    is_kolu: IsKolu   # yeni — iş kolu/kategori uyumsuzluğu validasyonu için şart
    is_anomali: bool = False
    anomali_turleri: List[str] = Field(default_factory=list)
    aciklama_kategorisi: str = ""   # yeni — ground truth (yeterli/yetersiz/manipulatif/ai_uretimi), JSON export'a (fatura_to_dict) DAHİL EDİLMEZ
    # onay_durumu BURADA YOK: o etiket Faz A'da (fatura nesnesi üzerinde) değil,
    # açiklama metni üretildikten SONRA JSON üzerinde atanir -> onay_durumu_ata.py.
    aciklama_metni: str = ""        # yeni — LLM tarafından doldurulacak, fatura_to_dict'e (model girdisi) dahil edilecek 

    @property
    def toplam_vergisiz_tutar(self) -> Decimal:
        """Tüm kalemlerin KDV hariç (iskonto sonrasi) toplami."""
        toplam = sum((k.ara_toplam for k in self.kalemler), Decimal("0"))
        return paraya_yuvarla(toplam)

    @property
    def toplam_kdv_tutari(self) -> Decimal:
        """Tüm kalemlerin KDV tutarlarinin toplami."""
        toplam = sum((k.kdv_tutari for k in self.kalemler), Decimal("0"))
        return paraya_yuvarla(toplam)

    @property
    def toplam_iskonto(self) -> Decimal:
        """Tüm kalemlerdeki iskonto tutarlarinin toplami (brüt - ara_toplam farki)."""
        toplam = Decimal("0")
        for k in self.kalemler:
            brut = Decimal(str(k.miktar)) * k.birim_fiyat
            iskonto_tutari = brut * Decimal(str(k.iskonto_orani)) / 100
            toplam += iskonto_tutari
        return paraya_yuvarla(toplam)

    @property
    def genel_toplam(self) -> Decimal:
        toplam = sum((k.satir_toplam for k in self.kalemler), Decimal("0"))
        return paraya_yuvarla(toplam)
    
class AnomaliliFaturaKalemi(FaturaKalemi):
    """
    FaturaKalemi'nin matematiksel anomali üretmek için kullanilan alt sinifi.
    sahte_satir_toplam verilirse, satir_toplam property'si; sahte_ara_toplam
    verilirse ara_toplam property'si gerçek hesaplama yerine bu sahte
    değerleri döndürür.
    """
    sahte_satir_toplam: Decimal | None = None
    sahte_ara_toplam: Decimal | None = None
    sahte_kdv_tutari: Decimal | None = None

    @property
    def ara_toplam(self) -> Decimal:
        if self.sahte_ara_toplam is not None:
            return self.sahte_ara_toplam
        return super().ara_toplam
    
    @property
    def kdv_tutari(self) -> Decimal:
        if self.sahte_kdv_tutari is not None:
            return self.sahte_kdv_tutari
        return super().kdv_tutari

    @property
    def satir_toplam(self) -> Decimal:
        if self.sahte_satir_toplam is not None:
            return self.sahte_satir_toplam
        return super().satir_toplam
    
class AnomaliliFatura(Fatura):
    """
    Fatura'nin genel_toplam'ini ya da footer kirilimlarindan
    (toplam_vergisiz_tutar / toplam_kdv_tutari) birini kasitli olarak
    bozmak için kullanilan alt sinifi.
    """
    sahte_genel_toplam: Decimal | None = None
    sahte_toplam_vergisiz_tutar: Decimal | None = None
    sahte_toplam_kdv_tutari: Decimal | None = None

    @property
    def toplam_vergisiz_tutar(self) -> Decimal:
        if self.sahte_toplam_vergisiz_tutar is not None:
            return self.sahte_toplam_vergisiz_tutar
        return super().toplam_vergisiz_tutar

    @property
    def toplam_kdv_tutari(self) -> Decimal:
        if self.sahte_toplam_kdv_tutari is not None:
            return self.sahte_toplam_kdv_tutari
        return super().toplam_kdv_tutari

    @property
    def genel_toplam(self) -> Decimal:
        if self.sahte_genel_toplam is not None:
            return self.sahte_genel_toplam
        return super().genel_toplam