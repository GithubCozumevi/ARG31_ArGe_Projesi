import random
from ortak.schema import Fatura

# 4 sabit açıklama kalite kategorisi. Bu modül asıl açıklama METNİNİ üretmiyor
# (o adım ayrı, LLM/qwen3 ile yapılacak) -- sadece o metnin hangi "davranış
# profiliyle" üretilmesi gerektiğine rule-based karar veriyor. Üretilecek
# metin bu karara göre kalibre edilmiş bir prompta yönlendirilecek.
ACIKLAMA_KATEGORILERI = ["yeterli", "yetersiz", "manipulatif", "ai_uretimi"]

# (anomali_turu veya "temiz") -> [Yeterli, Yetersiz, Manipülatif, AI Üretimi] ağırlıkları.
# Kalibrasyon kaynağı: Tornés et al. (ICDAR 2023) — sayısal/yapısal anomaliler
# (toplam, KDV) metinden bağımsız yakalanabiliyor, açıklamayla zayıf korelasyonlu;
# Thornton et al. (2025) — davranışsal/kategori anomalileri (yasaklı kategori,
# iş kolu uyumsuzluğu) çalışanın bilinçli kararına bağlı, açıklamada manipülasyon
# ihtimali çok daha yüksek. Bu ayrım A) sosyal/davranışsal ve B) yapısal/teknik
# olarak iki gruba ayrılıp buna göre kalibre edildi.
ACIKLAMA_KATEGORI_ORANLARI: dict[str, list[float]] = {
    "temiz":                          [60, 32, 2, 6],
    # A) Sosyal/davranışsal — çalışan farkında, manipülasyon ihtimali yüksek
    "yasakli_kategori":                [35, 10, 45, 10],
    "is_kolu_kategori_uyumsuzlugu":    [15, 15, 55, 15],
    "limit_asimi":                     [30, 15, 40, 15],
    "mukerrer_fis_yukleme":            [10, 10, 50, 30],
    "gelecek_tarihli":                 [40, 25, 20, 15],
    # B) Yapısal/teknik — çalışanın görüş alanı dışında, metinle zayıf korelasyon
    # fatura_no_cakismasi: satıcının numaralandırma hatası -> çalışanın görüş
    # alanı DIŞINDA, metinle korelasyonu zayıf; teknik dağılıma eşlenir.
    "fatura_no_cakismasi":             [55, 30, 5, 10],
    "footer_kismi":                    [55, 30, 5, 10],
    "kdv_tutari":                      [55, 30, 5, 10],
    # "kdv_kategori_uyumsuzlugu":        [50, 30, 10, 10], mevzuat değişimden ötürü kaldırıldı.
    "gecersiz_kimlik_no":              [58, 32, 3, 7],
    "genel_toplam":                    [45, 25, 20, 10],
    "satir_toplami":                   [50, 28, 12, 10],
}

# Fat-finger ondalık kaymaları (ondalik_kaymasi / dusuk_ondalik_kaymasi) açıklama
# metninden BAĞIMSIZDIR: hata, çalışan fotoğrafı yükleyip açıklamayı yazdıktan
# SONRA fiyat alanına girer (OCR yanlış okuması ya da muhasebecinin/çalışanın elle
# düzeltmesi). Bu yüzden açıklama, TEMİZ bir fişinki gibi yazılmıştır -> footer/kdv
# gibi "teknik" dağılıma değil, doğrudan "temiz" dağılımına eşlenirler (manipülasyona
# KAYMAZLAR). Ek fayda: sırf-sayısal bu anomalinin metin kanalına sızmasını önler
# (model onu açıklamadan değil, RAKAMDAN öğrenmek zorunda kalır -- leakage önleme).
ACIKLAMA_KATEGORI_ORANLARI["ondalik_kaymasi"] = list(ACIKLAMA_KATEGORI_ORANLARI["temiz"])
ACIKLAMA_KATEGORI_ORANLARI["dusuk_ondalik_kaymasi"] = list(ACIKLAMA_KATEGORI_ORANLARI["temiz"])

# Tabloda karşılığı olmayan türler (salt validator-kaynaklı sinsi matematiksel
# türler: basamak_karisikligi, sistematik_yuvarlama) için varsayılan "teknik/
# yapısal" dağılım -- footer_kismi/kdv_tutari ile ayni mantık: çalışanın fark
# edemeyeceği, metinle bağı zayıf.
VARSAYILAN_TEKNIK_ORAN = [55, 30, 5, 10]

# Çoklu-etiketli faturalarda (union etiketleme nedeniyle birden fazla tür
# ayni anda olabilir) hangi türün açıklama kategorisini belirleyeceğini
# seçen öncelik sırası -- en "insan bilinçli/kasıtlı" olandan en "teknik/
# fark edilmez" olana. Mantık: açıklamayı yazan kişi, faturadaki en yüksek
# "bilinç düzeyi" gerektiren soruna göre davranır.
ONCELIK_SIRASI = [
    "mukerrer_fis_yukleme",
    "yasakli_kategori",
    "limit_asimi",
    "is_kolu_kategori_uyumsuzlugu",
    "gelecek_tarihli",
    "genel_toplam",
    "satir_toplami",
    #"kdv_kategori_uyumsuzlugu",
    "kdv_tutari",
    "footer_kismi",
    "fatura_no_cakismasi",
    "gecersiz_kimlik_no",
]


def _belirleyici_turu_sec(anomali_turleri: list[str]) -> str:
    """
    Faturanin anomali_turleri listesinden, ONCELIK_SIRASI'na göre açıklama
    kategorisini belirleyecek TEK türü seçer. Fatura temizse "temiz" döner.
    ONCELIK_SIRASI'nda olmayan türlerden biri (varsa) tabloda kendi karşılığı
    yoksa VARSAYILAN_TEKNIK_ORAN'a düşülür (bkz. aciklama_kategorisi_belirle).
    """
    if not anomali_turleri:
        return "temiz"

    turler_seti = set(anomali_turleri)
    for tur in ONCELIK_SIRASI:
        if tur in turler_seti:
            return tur

    # ONCELIK_SIRASI'nda olmayan bir tür (sinsi matematiksel anomaliler vb.)
    # -- ismi döndürülür, oran sözlüğünde bulunamazsa aşağıda fallback devreye girer.
    return next(iter(turler_seti))


def aciklama_kategorisi_belirle(fatura: Fatura) -> str:
    """
    Faturanin (is_anomali/anomali_turleri) ground-truth etiketine göre,
    ACIKLAMA_KATEGORI_ORANLARI tablosundan ağırlıklı rastgele bir açıklama
    kalite kategorisi ("yeterli"/"yetersiz"/"manipulatif"/"ai_uretimi") seçer.

    ÖNEMLİ: Dönüş değeri GROUND TRUTH'tur -- LLM'e giden prompta hangi
    kategoriyi ÜRETMESİ gerektiğini söylemek için kullanılır, ama modele
    (fatura JSON'una, yani fatura_to_dict çıktısına) hiç sızmamalıdır.
    is_anomali/anomali_turleri ile ayni ilke (main.py:etiket_to_dict).
    """
    belirleyici_tur = _belirleyici_turu_sec(fatura.anomali_turleri)
    oranlar = ACIKLAMA_KATEGORI_ORANLARI.get(belirleyici_tur, VARSAYILAN_TEKNIK_ORAN)
    return random.choices(ACIKLAMA_KATEGORILERI, weights=oranlar, k=1)[0]


def veri_setine_aciklama_kategorisi_ata(faturalar: list[Fatura]) -> None:
    """Fatura listesindeki her faturaya (yerinde) aciklama_kategorisi atar."""
    for fatura in faturalar:
        fatura.aciklama_kategorisi = aciklama_kategorisi_belirle(fatura)


# ---------------------------------------------------------------------------
# ONAY DURUMU -- BU MODÜLDE DEĞİL
# ---------------------------------------------------------------------------
# `onay_durumu` (onaylandi/gozden_gecirilecek/onaylanmadi) etiketi buradan
# ÇIKARILDI ve `onay_durumu_ata.py`'ye taşındı. Sebep: muhasebe kararı ancak
# açıklama METNİ üretilip okunduktan SONRA verilebilir; burada (Faz A'da,
# kategori atamasıyla birlikte) üretmek "önce karar, sonra açıklama" gibi ters
# bir nedensellik kuruyordu. Karar tablosu ve gerekçesi için o modüle bak.