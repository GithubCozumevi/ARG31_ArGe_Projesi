"""
`data/urun_verileri/temiz_urunler.csv` içindeki YANLIŞ kategorilenmiş ürünleri
yeniden etiketler / ayıklar.

SORUN (ölçüldü 2026-07-28): `yazilim_lisans` havuzundaki 620 ürünün yalnızca ~%12'si
gerçekten yazılım/lisans. Kalanı e-ticaret verisinden gelen YKS/LGS hazırlık setleri,
kişisel koçluk kursları, hediye kartları, ön ödemeli kartlar ve dernek bağış
sertifikaları. Bu, açıklama üretimini ONARILAMAZ biçimde bozuyor: prompt'a
"Online Gitar Eğitimi 1 Aylık (yazilim lisans)" gibi kendi kendini yalanlayan bir
kalem giriyor, model ne yazsa saçma oluyor. Etiketleri de bozuyor:
`is_kolu_kategori_uyumsuzlugu` anomalisi kategori semantiğine dayanıyor.

Kirlilik TEK kategoride izole: diğer 5 kategoride şüpheli oran <%0,2 (ölçüldü).

YAKLAŞIM -- BEYAZ LİSTE: çöp sonsuz biçimde, sağlam dar. Kurumsal masrafa uyan üç
kova tanımlanır, KALANI SİLİNİR. (Kara liste denendi: 'eğitim' kelimesiyle ayıklama
"Sigarayı Bırakma Video Eğitimi" gibi tüketici kurslarını kurumsal eğitim sanıyordu.)

  yazilim_lisans   : gerçek yazılım/güvenlik/lisans/SaaS -> KALIR
  ofis_sarf_malzeme: dijital kartvizit -> meşru kurumsal alım, havuzu da çok ince
  eglence          : hediye kartı/kodu, dijital içerik aboneliği -> YASAKLI kategori;
                     kurumsal fişte görünmesi tam da simüle etmek istediğimiz ihlal
  (kural yok)      : SİLİNİR -- sınav hazırlık, kişisel koçluk, ön ödemeli kart,
                     bağış sertifikası, kuluçka makinesi... kurumsal karşılığı yok

Kullanım:
    python -m faz_a_fis_uretimi.urun_kategori_duzelt                # SADECE rapor (dosyaya dokunmaz)
    python -m faz_a_fis_uretimi.urun_kategori_duzelt --uygula       # yedek alıp CSV'yi günceller
"""

import argparse
import csv
import re
import shutil
from collections import Counter
from pathlib import Path

VARSAYILAN_CSV = "data/urun_verileri/temiz_urunler.csv"
HEDEF_KATEGORI = "yazilim_lisans"   # yalnız bu kategorinin satırları elden geçirilir

SIL = "__SIL__"


def _d(*kaliplar: str) -> re.Pattern:
    return re.compile("|".join(kaliplar), re.IGNORECASE)


# BEYAZ LİSTE yaklaşımı: çöpü saymak yerine SAĞLAMI tanımla, gerisini sil.
# Kara liste denendi ve çürük çıktı -- 'eğitim' kelimesiyle ayıklamak
# "Sigarayı Bırakma Video Eğitimi", "Kore Mutfağı Yemek Tarifleri Eğitimi",
# "Amazon KDP ile Kitap Pazarlama" gibi TÜKETİCİ kurslarını kurumsal eğitim sanıyordu.
# Bu havuz e-ticaret verisinden geldiği için çöpün biçimi sonsuz; sağlamınki dar.
#
# SIRA ÖNEMLİ: 'Bitdefender ... + App Store Hediye Kodu' gerçek bir antivirüs
# lisansıdır, hediye-kartı desenine düşmemeli -> yazılım kuralı en üstte.
KURALLAR: list[tuple[str, re.Pattern]] = [
    # 0) OYUN/KONSOL -> eglence. Yazılım kuralından ÖNCE gelmeli: 'Microsoft Xbox Game
    #    Pass' yazılım desenine ('microsoft') takılıyordu. Desen DAR tutuldu -- 'hediye
    #    kodu' burada YOK, yoksa 'Bitdefender + App Store Hediye Kodu' buraya düşerdi.
    ("eglence", _d(r"xbox|playstation|\bps[45]\b|nintendo|game pass|steam\b|oyun kodu")),
    # 1) Gerçek yazılım / güvenlik / lisans / kurumsal SaaS -> KALIR
    ("yazilim_lisans", _d(
        r"bitdefender", r"kaspersky", r"norton", r"\beset\b", r"avast", r"mcafee",
        r"malwarebytes", r"antivir", r"internet security", r"total security",
        r"endpoint|gravityzone", r"\blisans", r"yazılım|yazilim", r"\bsaas\b",
        # 'office' TEK BAŞINA yeter: ürün adlarında yalnız MS Office ailesinde geçiyor
        # (tarandı). Eskiden 'office 365' isteniyordu ve 'Ms Office 2019', 'Office 2021
        # Pro Plus', 'COREL Draw ... Ticari Sürüm' gibi GERÇEK yazılımlar siliniyordu.
        r"\boffice\b", r"microsoft", r"windows \d", r"corel", r"photoshop|acrobat|visio",
        r"vmware|veeam|sql server|oracle|solidworks", r"adobe", r"autocad",
        r"\bapi kullan", r"bulut depolama", r"sunucu lisans", r"veri taban",
        # DİKKAT: 'kod' TEK BAŞINA yazılamaz -- 'Google Play Hediye Kodu'nu yakalayıp
        # eglence kuralının önüne geçiyordu. Yalnız yazılıma özgü birleşimler.
        r"\bcrm\b|\berp\b", r"canva pro", r"lisans kod|ürün anahtar|product key",
        r"\boem\b", r"kullanıcı \d+ yıl|\d+ kullanıcı",
    )),
    # 2) Dijital kartvizit -> kurumsal sarf malzemesi. Meşru bir şirket alımı ve
    #    ofis_sarf_malzeme havuzu çok ince (5 jenerik girdi), doldurması iyi olur.
    ("ofis_sarf_malzeme", _d(r"kartvizit")),
    # 3) Hediye kartı/kodu + dijital içerik aboneliği -> eglence (YASAKLI kategori).
    #    Kurumsal fişte Netflix kodu görünmesi tam da simüle etmek istediğimiz ihlal;
    #    eglence havuzu da 4 jenerik girdiden ibaret, gerçek ürün adı kazandırır.
    ("eglence", _d(
        r"hediye kart|hediye kodu|hediye çeki", r"google play", r"app store", r"itunes",
        r"netflix|spotify|duolingo|tinder", r"cineverse|moviepass|sinema",
        r"s sport|bein|exxen|blutv", r"oyun kodu|steam|playstation|xbox",
        r"premium üyelik|premium \d+ aylık|\d+ aylık abonelik",
    )),
]
# Yukarıdaki üç kuralın HİÇBİRİNE uymayan her satır SİLİNİR (varsayılan).
# Gerekçe: kurumsal masraf simülasyonunda YKS hazırlık seti, kişisel koçluk kursu,
# ön ödemeli kart, dernek bağış sertifikası, kuluçka makinesi vb. hiçbir senaryoda
# anlamlı değil -- ne kalem olarak ne anomali olarak.


def kategori_belirle(ad: str) -> str:
    """Ürün adına göre hedef kategori; hiçbir kurala uymayan SİLİNİR."""
    for hedef, desen in KURALLAR:
        if desen.search(ad):
            return hedef
    return SIL


def main():
    p = argparse.ArgumentParser(description="Yanlış kategorilenmiş ürünleri yeniden etiketle")
    p.add_argument("--csv", default=VARSAYILAN_CSV)
    p.add_argument("--kategori", default=HEDEF_KATEGORI, help="elden geçirilecek kaynak kategori")
    p.add_argument("--uygula", action="store_true",
                   help="CSV'yi GERÇEKTEN güncelle (varsayılan: sadece rapor)")
    p.add_argument("--ornek", type=int, default=4, help="kova başına gösterilecek örnek sayısı")
    args = p.parse_args()

    yol = Path(args.csv)
    with open(yol, encoding="utf-8-sig", newline="") as f:
        okuyucu = csv.DictReader(f)
        alanlar = okuyucu.fieldnames or []
        satirlar = list(okuyucu)

    hedefler = [s for s in satirlar if s["harcama_kategorisi"] == args.kategori]
    print(f"[+] {yol}: {len(satirlar)} satır, '{args.kategori}' etiketli {len(hedefler)}\n")

    kovalar: dict[str, list[str]] = {}
    for s in hedefler:
        kovalar.setdefault(kategori_belirle(s["title"]), []).append(s["title"])

    sirali = sorted(kovalar.items(), key=lambda x: -len(x[1]))
    for hedef, urunler in sirali:
        etiket = "SİLİNECEK" if hedef == SIL else (
            "KALIYOR" if hedef == args.kategori else f"-> {hedef}")
        print(f"  {etiket:22s} {len(urunler):4d} (%{len(urunler)/len(hedefler)*100:4.1f})")
        for u in urunler[:args.ornek]:
            print(f"      · {u[:96]}")
    kalan = len(kovalar.get(args.kategori, []))
    print(f"\n[+] Sonuç: '{args.kategori}' havuzu {len(hedefler)} -> {kalan} ürün")
    print("    NOT: IS_KOLU_AGIRLIKLARI havuz uzunluğunun LOG'una bağlı "
          "(TABAN + log1p(n)) -> dağılım etkisi ~%1, ihmal edilebilir.")

    if not args.uygula:
        print("\n[!] RAPOR MODU -- dosyaya dokunulmadı. Uygulamak için: --uygula")
        return

    yedek = yol.with_suffix(yol.suffix + ".yedek")
    shutil.copy2(yol, yedek)
    yeni: list[dict] = []
    sayac = Counter()
    for s in satirlar:
        if s["harcama_kategorisi"] != args.kategori:
            yeni.append(s)
            continue
        hedef = kategori_belirle(s["title"])
        if hedef == SIL:
            sayac["silindi"] += 1
            continue
        if hedef and hedef != args.kategori:
            s["harcama_kategorisi"] = hedef
            sayac[f"-> {hedef}"] += 1
        yeni.append(s)

    with open(yol, "w", encoding="utf-8", newline="") as f:
        yazici = csv.DictWriter(f, fieldnames=alanlar)
        yazici.writeheader()
        yazici.writerows(yeni)

    print(f"\n[+] Yedek  -> {yedek}")
    print(f"[+] Yazıldı -> {yol}  ({len(satirlar)} -> {len(yeni)} satır)")
    for k, v in sayac.most_common():
        print(f"      {k}: {v}")
    print("\n[!] SIRADAKİ: 'python -m faz_a_fis_uretimi.main --count 100000 --anomali-orani 0.25 "
          "--output-dir data --filename faturalar' (DESTRUCTIVE) + pilot_set_hazirla.py")


if __name__ == "__main__":
    main()
