"""
Gold (etiketli) JSON klasorunden satici sozlugu uretir.

Kullanim (proje kok klasorunden, venv aktifken):
  python -m scripts.arac.satici_sozlugu_olustur "C:\\...\\json3"

Cikti: data\\satici_sozlugu.json
Sonra servise: POST /extract/sozluk-yenile (ya da servisi yeniden baslat).
"""
import sys

from app.services.vendors import sozluk_olustur, sozluk_kaydet


def main():
    if len(sys.argv) != 2:
        print('Kullanim: python -m scripts.arac.satici_sozlugu_olustur "<gold json klasoru>"')
        sys.exit(1)

    sozluk, o = sozluk_olustur(sys.argv[1])
    yol = sozluk_kaydet(sozluk)

    print(f"Okunan dosya           : {o['okunan_dosya']}")
    print(f"Sozluge giren fis      : {o['kullanilan_fis']}")
    print(f"Atlanan dosya          : {o['atlanan_dosya']}")
    for neden, adet in o["atlama_nedenleri"].items():
        print(f"   - {neden}: {adet}")
    print()
    print(f"Benzersiz satici       : {o['benzersiz_satici']}")
    print(f"   tek fisli satici    : {o['tek_fisli_satici']}")
    print(f"   tekrar eden satici  : {o['tekrar_eden_satici']}")
    n = o["kullanilan_fis"] or 1
    print(f"Sozlukten fayda gorebilecek fis: {o['faydalanabilecek_fis']} / {o['kullanilan_fis']} "
          f"(%{100 * o['faydalanabilecek_fis'] / n:.0f})")
    print(f"Checksum'i gecmeyen VKN: {o['checksum_gecersiz_vkn']}  (gold'daki yazim hatasi olabilir)")
    print(f"TCKN'li (sahis) satici : {o['tckn_satici']}")
    print(f"Kaydedildi             : {yol}")


if __name__ == "__main__":
    main()
