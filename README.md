# ARG31 Ar-Ge Projesi: Masraf Denetim

Masraf Denetim, çalışanların harcama fişlerini (masraf) otomatik okuyup denetleyen bir yapay zeka projesidir.
Fişten firma adı, satıcı VKN/TCKN, tarih ve toplam tutar çıkarılır; emin olunamayan fişler **insan kontrolüne** yönlendirilir.

Bu repo projenin farklı parçalarını tek yerde toplar. Her klasörün kendi `README.md` dosyası vardır; kurulum ve kullanım ayrıntıları orada yazar.

## Klasörler

| Klasör | Ne işe yarar | Başlangıç |
| --- | --- | --- |
| [`masraf-ml-services/`](masraf-ml-services/) | OCR metninden VKN/TCKN çıkaran hibrit karar akışı, FastAPI servisi, ölçüm ve veri denetim betikleri, layout araştırması | [README](masraf-ml-services/README.md) |
| [`infra/`](infra/) | Yerel altyapı: fiş görsellerini ve model dosyalarını saklayan **MinIO**, deney takibi yapan **MLflow** (Docker ile) | [README](infra/README.md) |
| [`sentetik_fis_uretimi/`](sentetik_fis_uretimi/) | Eğitim ve test için sentetik fiş üreten kodlar (fiş içeriği, açıklama metinleri, fiş görselleştirme) | [README](sentetik_fis_uretimi/README.md) |
| `arayuz/` | Kullanıcı arayüzü (henüz eklenmedi) | - |

## Parçalar nasıl birbirine bağlanır?

```
sentetik_fis_uretimi ──► sentetik fiş görselleri ve etiketleri ─┐
                                                                ├─► MinIO (infra) ─► model eğitimi / ölçüm (MLflow, infra)
gerçek fiş görselleri (repoda yok) ─────────────────────────────┘
                                                                
fiş görseli ─► OCR + VLM (Colab/Kaggle) ─► masraf-ml-services (VKN/firma/tarih/tutar + "insan kontrolü?") ─► arayuz
```

- **Sentetik fişler** modelleri eğitmek ve denemek için üretilir. Temiz ve şablon tabanlıdır, gerçek fotoğraf fişlerinin yerini tutmaz.
- **`masraf-ml-services`** OCR/VLM çıktısını alır, VKN'yi checksum ve satıcı sözlüğüyle doğrular, güvenilmeyenleri insana yollar.
- **`infra`** görselleri ve deney kayıtlarını saklar. Servislerin çalışması için Docker Desktop açık olmalıdır.

## Neler repoda yok?

Aşağıdakiler **bilerek** repoya yüklenmez:

- Şifreler, access key'ler ve `.env` dosyaları. Her klasörde `.env.example` vardır; kendi `.env` dosyanı bundan oluşturursun.
- Gerçek fiş görselleri, OCR/VLM çıktıları ve etiketler (gerçek VKN, TCKN ve firma bilgisi içerir).
- Sentetik fiş görsellerinin kendisi (yaklaşık 43 bin dosya, 700 MB üstü zip). Veri setinin nereden alınacağını ilgili ekipten ya da proje belgelerinden öğren.
- Sanal ortam (`venv/`), `__pycache__/`, `data/` altındaki üretilmiş çıktılar.

## Başlarken

1. Repoyu indir: `git clone https://github.com/GithubCozumevi/ARG31_ArGe_Projesi.git`
2. İlgilendiğin klasöre gir ve README'sini oku.
3. Servisi denemek için: `masraf-ml-services/README.md` → "Kurulum ve çalıştırma".
4. MinIO / MLflow gerekiyorsa önce Docker Desktop'ı aç, sonra `infra/README.md` adımlarını izle.

## Durum

Araştırma ve pilot aşamasındadır; servis üretime hazır değildir. Ayrıntılı ölçüm sonuçları ve bilinen sınırlar `masraf-ml-services/README.md` içindedir.
