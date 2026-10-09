"""
Fiş dizinindeki FAZLA PNG'leri (nihai veri setinde olmayan `kayit_id`'ler) temizler.

NEDEN GEREKLİ: fişler `faturalar.json`'dan render edilir ve Faz B'den bağımsızdir,
dolayisiyla dizin nihai veri setinden GENİŞ olabilir. `aciklama_birlestir.py --eleme`
ile elenen ve üretim sonrasi silinen kayitlarin PNG'si dizinde öksüz kalir.

ÜYELİK KAYNAĞI model girdisidir (`faturalar_aciklamali.json`), batch çikti dizini
DEĞİL: eleme orada uygulaniyor ve `onay_durumu_ata.py` de üyeligi ayni dosyadan
okuyor. Üç tüketicinin ayni kaynagi kullanmasi tutarliligi garanti eder.

Varsayilan RAPOR modu (urun_kategori_duzelt.py ile ayni kalip); `--uygula` siler.
`--tasi-dizin` verilirse silmek yerine tasir (geri alinabilir).

GÜVENLİK: beklenenden fazla dosya silinecekse (>%5) `--zorla` olmadan durur --
yanlis referans dosyasi vermek (ör. henüz --eleme ile birlestirilmemis surum)
sessizce binlerce fisi silebilirdi.

    python -m faz_c_fis_gorsellestirme.fis_fazla_temizle --fis-dizini data/fisler_25k_2
    python -m faz_c_fis_gorsellestirme.fis_fazla_temizle --fis-dizini data/fisler_25k_2 --uygula
"""

import argparse
import json
import shutil
from pathlib import Path

VARSAYILAN_GIRDI_JSON = "data/faturalar_aciklamali.json"
GUVENLIK_ORANI = 0.05


def veri_seti_idleri(girdi_json: str) -> set[str]:
    with open(girdi_json, "r", encoding="utf-8") as f:
        return {k["kayit_id"] for k in json.load(f)}


def fazla_pngleri_bul(fis_dizini: Path, idler: set[str]) -> tuple[list[Path], int]:
    """(fazla dosyalar, toplam png). Dosya adinin govdesi `kayit_id`dir."""
    tum = sorted(fis_dizini.glob("*.png"))
    return [p for p in tum if p.stem not in idler], len(tum)


def main() -> None:
    ap = argparse.ArgumentParser(description="Veri setinde olmayan fiş PNG'lerini temizle")
    ap.add_argument("--fis-dizini", required=True, help="PNG'lerin bulundugu dizin")
    ap.add_argument("--girdi-json", default=VARSAYILAN_GIRDI_JSON,
                    help="uyelik kaynagi: aciklama_birlestir.py ciktisi")
    ap.add_argument("--uygula", action="store_true", help="Gercekten sil (varsayilan: rapor)")
    ap.add_argument("--tasi-dizin", default=None,
                    help="Silmek yerine bu dizine tasi (geri alinabilir)")
    ap.add_argument("--zorla", action="store_true",
                    help="Guvenlik esigini (%%5) yok say")
    args = ap.parse_args()

    fis_dizini = Path(args.fis_dizini)
    if not fis_dizini.is_dir():
        print(f"HATA: dizin yok: {fis_dizini}")
        return
    if not Path(args.girdi_json).exists():
        print(f"HATA: uyelik kaynagi yok: {args.girdi_json}\n"
              f"     once `aciklama_birlestir.py` kosulmali.")
        return

    idler = veri_seti_idleri(args.girdi_json)
    fazla, toplam = fazla_pngleri_bul(fis_dizini, idler)
    eksik = len(idler - {p.stem for p in fis_dizini.glob("*.png")})

    print(f"[+] Veri seti      : {len(idler)} kayit ({args.girdi_json})")
    print(f"[+] Dizindeki PNG  : {toplam} ({fis_dizini})")
    print(f"[+] FAZLA (silinir): {len(fazla)}")
    print(f"[+] PNG'si EKSİK   : {eksik}" + ("  <-- render tamamlanmamis!" if eksik else ""))

    if not fazla:
        print("\n[+] Silinecek dosya yok.")
        return

    print("\n    ilk 10 ornek:", ", ".join(p.name for p in fazla[:10]))

    oran = len(fazla) / toplam if toplam else 0
    if oran > GUVENLIK_ORANI and not args.zorla:
        print(f"\n[!] DURDURULDU: dizinin %{100 * oran:.1f}'i silinecekti (esik %{100 * GUVENLIK_ORANI:.0f}).")
        print("    Yanlis --girdi-json vermis olabilirsin (ornegin --eleme ile")
        print("    birlestirilmemis eski surum). Dogruysa --zorla ile tekrarla.")
        return

    if not args.uygula:
        print("\n[i] RAPOR modu, hicbir dosyaya dokunulmadi. Uygulamak icin: --uygula")
        return

    if args.tasi_dizin:
        hedef = Path(args.tasi_dizin)
        hedef.mkdir(parents=True, exist_ok=True)
        for p in fazla:
            shutil.move(str(p), str(hedef / p.name))
        print(f"\n[+] {len(fazla)} dosya tasindi -> {hedef}")
    else:
        for p in fazla:
            p.unlink()
        print(f"\n[+] {len(fazla)} dosya silindi.")
    print(f"[+] Dizinde kalan PNG: {len(list(fis_dizini.glob('*.png')))}")


if __name__ == "__main__":
    main()
