import argparse
import json
import csv
from pathlib import Path
from ortak.validators import (
    dogrulama_raporu_olustur, raporu_yazdir, vkn_firma_tutarlilik_hatalarini_bul,
    kural_ihlali_turlerini_tespit_et, veri_seti_dogrula,
)
from faz_a_fis_uretimi.generators.field_generator import rastgele_fatura
from faz_a_fis_uretimi.generators.anomaly_injector import karisik_veri_seti_uret
from faz_a_fis_uretimi.generators.aciklama_uretici import veri_setine_aciklama_kategorisi_ata


def fatura_to_dict(fatura) -> dict:
    """Pydantic modelini JSON-uyumlu dict'e çevirir, hesaplanan alanlari da ekler."""
    return {
        "kayit_id": fatura.kayit_id,   # benzersiz satir anahtari (join icin; ÖZELLİK DEĞİL)
        "fatura_no": fatura.fatura_no,
        "fatura_tarihi": fatura.fatura_tarihi,
        "yukleme_zamani": fatura.yukleme_zamani,   # model girdisi, leakage DEĞİL
        "saat": fatura.saat,                       # fişin üzerinde basılı
        "satici_vkn": fatura.satici_vkn,
        "satici_unvan": fatura.satici_unvan,
        "adres": fatura.adres,
        # Sahis sirketinde isletmenin sahibi; tuzelde bos. Fiste basili -> model
        # girdisi (bkz. schema.Fatura.satici_sahis_adi).
        "satici_sahis_adi": fatura.satici_sahis_adi,
        # is_kolu + kalem bazlı harcama_kategorisi etiket_to_dict'e taşındı.
        "toplam_kdv_tutari": float(fatura.toplam_kdv_tutari),
        "genel_toplam": float(fatura.genel_toplam),
        "kalemler": [
            {
                "kalem_no": k.kalem_no,
                "aciklama": k.aciklama,
                "miktar": k.miktar,
                "birim": k.birim,
                "birim_fiyat": float(k.birim_fiyat),
                "kdv_orani": k.kdv_orani,
                "satir_toplam": float(k.satir_toplam),
            }
            for k in fatura.kalemler
        ],
    }

def etiket_to_dict(fatura) -> dict:
    """
    Modelin GÖRMEMESİ gereken ground-truth etiket bilgisini ayri bir
    dict'e çevirir. fatura_to_dict()'ten kasitli olarak ayri tutulur —
    veri sizintisini (data leakage) önlemek için etiketler modelin
    girdisine hiç dahil edilmemeli, sadece değerlendirme/eğitim etiketi
    olarak kullanilmali.
    """
    return {
        "kayit_id": fatura.kayit_id,
        "fatura_no": fatura.fatura_no,
        "is_kolu": fatura.is_kolu.value,
        "harcama_kategorileri": [k.harcama_kategorisi.value for k in fatura.kalemler],
        "is_anomali": fatura.is_anomali,
        "anomali_turleri": fatura.anomali_turleri,
        "aciklama_kategorisi": fatura.aciklama_kategorisi,
        # onay_durumu BİLEREK BURADA YOK: o etiket açıklama METNİ üretildikten
        # SONRA, Faz B'nin son adımında atanır (onay_durumu_ata.py) ve ayrı bir
        # etiket dosyasına (faturalar_aciklamali_etiketler.json) yazılır.
    }


def etiketleri_kaydet(faturalar, dosya_yolu: Path) -> None:
    etiketler = [etiket_to_dict(f) for f in faturalar]
    with open(dosya_yolu, "w", encoding="utf-8") as f:
        json.dump(etiketler, f, ensure_ascii=False, indent=2)

#faturalarin üretiminde artik fatura_to_dict fonksiyonu kullaniliyor, 
#çünkü pydantic modelini JSON uyumlu dict'e çeviriyor ve hesaplanan alanlari da ekliyor.
#bu fonksiyon çağrilmiyor doğrulanmadan fatura üretimi gerekirse diye kalsin.
def faturalari_uret(adet: int) -> list[dict]:
    """`adet` kadar rastgele fatura üretir ve dict listesine çevirir."""
    return [fatura_to_dict(rastgele_fatura()) for _ in range(adet)]


def json_olarak_kaydet(faturalar: list[dict], dosya_yolu: Path) -> None:
    with open(dosya_yolu, "w", encoding="utf-8") as f:
        json.dump(faturalar, f, ensure_ascii=False, indent=2)


def csv_olarak_kaydet(faturalar: list[dict], dosya_yolu: Path) -> None:
    """Her kalemi bir satir olarak yazar; fatura bilgileri her satirda tekrarlanir."""
    satirlar = []
    for fatura in faturalar:
        for kalem in fatura["kalemler"]:
            satirlar.append({
                "fatura_no": fatura["fatura_no"],
                "fatura_tarihi": fatura["fatura_tarihi"],
                "satici_vkn": fatura["satici_vkn"],
                "satici_unvan": fatura["satici_unvan"],
                **kalem,
                "fatura_genel_toplam": fatura["genel_toplam"],
            })

    if not satirlar:
        return

    with open(dosya_yolu, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=satirlar[0].keys())
        writer.writeheader()
        writer.writerows(satirlar)


def main():
    parser = argparse.ArgumentParser(description="Sentetik Türkçe fatura üretici")
    parser.add_argument("--count", type=int, default=100, help="Üretilecek fatura sayisi")
    parser.add_argument("--output-dir", type=str, default="data", help="Çikti klasörü")
    parser.add_argument("--filename", type=str, default="faturalar", help="Dosya adi (uzantisiz)")
    parser.add_argument("--anomali-orani", type=float, default=0.0, help="0.0-1.0 arasi, anomalili fatura orani")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"{args.count} adet sentetik fatura üretiliyor...")

    # Tek seferde üret, hem doğrulama hem export bu tek listeyi kullansin
    fatura_nesneleri = karisik_veri_seti_uret(args.count, args.anomali_orani)

    # Benzersiz satir anahtari. fatura_no benzersiz olmadigi icin (mukerrer_fis_yukleme /
    # fatura_no_cakismasi) aciklama boru hatti bununla anahtarlanir -- bkz. schema.Fatura.
    for i, f in enumerate(fatura_nesneleri):
        f.kayit_id = f"K{i:07d}"


    # Union (Additive) Etiketleme: üreticinin enjekte ettiği anomali
    # etiketlerine, doğrulayıcının BAĞIMSIZ tespit ettiği kural ihlallerini
    # birleştiriyoruz -- böylece bir anomalinin yan etkisi ya da doğal
    # varyans kaynaklı kural ihlalleri manuel yama gerekmeden etiketlenir.
    for f in fatura_nesneleri:
        otonom_turler = kural_ihlali_turlerini_tespit_et(f)
        if otonom_turler:
            f.anomali_turleri = list(set(f.anomali_turleri) | otonom_turler)
            f.is_anomali = True

    # Açıklama kategorisi ataması, anomali_turleri TAMAMEN belirlendikten
    # (union etiketleme bittikten) SONRA yapılmalı -- aksi halde
    # ONCELIK_SIRASI eksik bilgiyle çalışır.
    veri_setine_aciklama_kategorisi_ata(fatura_nesneleri)

    # onay_durumu ataması BURADA YAPILMAZ (eskiden yapılıyordu). Muhasebe kararı
    # ancak açıklama METNİ okunduktan sonra verilebilir -> Faz B'nin son adımında,
    # onay_durumu_ata.py ile atanır. Ollama bu etiketi hiçbir aşamada görmez.

    # VKN-firma tutarlilik GÜVENLİK KONTROLÜ (artık veri SİLMEZ).
    # Firma registry mimarisiyle (firma_registry_olustur.py) her ad→sabit VKN ve
    # her VKN→sabit ad garantisi İNŞA gereği sağlanır; dolayısıyla bu kontrol
    # normalde SIFIR çelişki bulmalı. Eskiden burada çelişkili faturalar SİLİNİYORDU
    # (100k üretimde ~25k fatura eleniyordu, fatura_no seti nondeterministik oluyordu);
    # registry bu sorunu ortadan kaldırdığı için artık silmiyoruz -- sadece beklenmedik
    # bir çelişki çıkarsa (registry bütünlüğü bozulmuşsa) GÜRÜLTÜLÜ uyarı veriyoruz.
    # Ayni-fatura-no anomalileri (mukerrer_fis_yukleme / fatura_no_cakismasi) VKN+unvan'ı
    # BİRLİKTE eşitlediği için çelişki üretmez;
    # yine de muafiyeti koruyoruz (ör. gecersiz_kimlik_no rastgele VKN çakışması).
    #
    # DÜZELTME: `korunan_adlar` eskiden yalnız ayni-fatura-no türlerini kapsıyordu ve
    # gecersiz_kimlik_no ADI muaf tutmuyordu. O anomali VKN'yi bozup UNVANI aynı
    # bıraktığı için, aynı firmanın gerçek VKN'li diğer faturalarıyla sahte bir
    # "aynı ad / farklı VKN" çelişkisi doğuyordu (100k koşuda 1208 ad, 6599 fatura --
    # 4560'ı hiç anomalisi olmayan tertemiz fatura). Etiketlere hiç sızmıyordu ama
    # raporda "Hatalı Fatura"yı gerçek anomali sayısının ~4560 üstüne çıkarıyordu.
    # Aynı muafiyet validators.KIMLIK_MUAF_ANOMALILER ile rapor tarafında da var.
    korunan_vknler = {
        f.satici_vkn for f in fatura_nesneleri
        if not {"fatura_no_cakismasi", "mukerrer_fis_yukleme", "gecersiz_kimlik_no"}.isdisjoint(f.anomali_turleri)
    }
    korunan_adlar = {
        f.satici_unvan for f in fatura_nesneleri
        if not {"fatura_no_cakismasi", "mukerrer_fis_yukleme", "gecersiz_kimlik_no"}.isdisjoint(f.anomali_turleri)
    }

    vkn_firma_hatalari = vkn_firma_tutarlilik_hatalarini_bul(fatura_nesneleri)
    celiskili_adlar = set(vkn_firma_hatalari["ayni_ad_farkli_vkn"].keys()) - korunan_adlar
    celiskili_vknler = set(vkn_firma_hatalari["ayni_vkn_farkli_ad"].keys()) - korunan_vknler
    elenen_fatura_sayisi = 0   # registry mimarisi: fatura ELENMİYOR (deterministik fatura_no seti)
    if celiskili_adlar or celiskili_vknler:
        print(f"[!] UYARI: registry mimarisiyle beklenmeyen VKN-firma çelişkisi bulundu "
              f"(ayni_ad_farkli_vkn={len(celiskili_adlar)}, ayni_vkn_farkli_ad={len(celiskili_vknler)}). "
              f"Fatura SİLİNMEDİ; registry bütünlüğünü kontrol et (firma_registry.csv).")
    rapor = dogrulama_raporu_olustur(fatura_nesneleri)
    rapor["hedef_anomali_orani"] = args.anomali_orani
    rapor["talep_edilen_fatura_adedi"] = args.count
    rapor["elenen_fatura_sayisi_vkn_tutarsizlik"] = elenen_fatura_sayisi
    raporu_yazdir(rapor)

    # Union etiketlemeden SONRA çağrilmali; yalniz uyarir, üretimi durdurmaz.
    uyarilar = veri_seti_dogrula(fatura_nesneleri, args.anomali_orani)
    rapor["veri_seti_uyarilari"] = uyarilar
    if uyarilar:
        print(f"\n[!] VERİ SETİ UYARILARI ({len(uyarilar)}):")
        for u in uyarilar:
            print(f"    - {u}")
    else:
        print("\n[+] Veri seti invariant kontrolü temiz (uyari yok).")

    faturalar = [fatura_to_dict(f) for f in fatura_nesneleri]

    json_yolu = output_dir / f"{args.filename}.json"
    csv_yolu = output_dir / f"{args.filename}.csv"

    json_olarak_kaydet(faturalar, json_yolu)
    csv_olarak_kaydet(faturalar, csv_yolu)

    etiket_yolu = output_dir / f"{args.filename}_etiketler.json"
    etiketleri_kaydet(fatura_nesneleri, etiket_yolu)

    toplam_kalem = sum(len(f["kalemler"]) for f in faturalar)

    print(f"Tamamlandi:")
    print(f"  {len(faturalar)} fatura, {toplam_kalem} kalem üretildi")
    print(f"  JSON -> {json_yolu}")
    print(f"  CSV  -> {csv_yolu}")
    print(f"  Etiketler -> {etiket_yolu}")
    rapor_yolu = output_dir / f"{args.filename}_rapor.json"
    with open(rapor_yolu, "w", encoding="utf-8") as f:
        json.dump(rapor, f, ensure_ascii=False, indent=2, default=list)


if __name__ == "__main__":
    main()