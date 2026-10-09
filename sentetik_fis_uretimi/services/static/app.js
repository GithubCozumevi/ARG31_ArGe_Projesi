const folderInput = document.getElementById("folder-input");
const sourceStatus = document.getElementById("source-status");
const splitTabsEl = document.getElementById("split-tabs");
const searchInput = document.getElementById("search-input");
const filterToggle = document.getElementById("anomaly-filter-toggle");
const filterOptionsEl = document.getElementById("anomaly-filter-options");
const resultListEl = document.getElementById("result-list");
const answerEl = document.getElementById("result");

// splitName -> { records: Map(kayitId -> record), pairGroups: Map(pairKey -> [kayitId,...]),
//                turSayaci: Map(tur -> count),
//                consistencyCache: Map(kayitId -> {durum, tutarli, turler}) }
let splits = {};
let activeSplit = null;
const activeAnomaliFilter = new Set(); // secili tur adlari (coklu secim)
let selectedKayitId = null;
let sonucRenderNesli = 0; // liste yeniden render edilince eski async doğrulama yazımlarını iptal eder

function pairKeyOf(rec) {
  return `${rec.satici_vkn}::${rec.fatura_no}`;
}

function kayitIdNormalize(input) {
  const temiz = input.trim().replace(/^[Kk]/, "");
  if (!/^\d+$/.test(temiz)) return null;
  return "K" + temiz.padStart(7, "0");
}

// --- final_veriler_en adaptörü --------------------------------------------
// data/final_veriler_en (İngilizce alan adları + hashlenmiş record_id) klasörü
// yüklendiğinde, ORİJİNAL dosyalara dokunmadan, yalnız bellekte final_veriler'in
// (Türkçe) şekline çevirir -- alttaki tüm kod (merge, arama, /validate) TEK
// şema (Türkçe) bilir, ikinci bir yol eklemez. alan_adlari.py'nin ürettiği
// eşlemelerin BİREBİR TERSİ (iki kaynak sapmasın diye tabloları oradan kopyala,
// yeniden icat etme).
const EN_BUSINESS_LINE = {
  restaurant: "restoran", grocery_store: "market", hotel: "otel",
  office_supplies_store: "ofis_tedarik", technology: "teknoloji",
  consulting_firm: "danismanlik_firmasi", logistics_firm: "lojistik_firmasi",
  transport_provider: "ulasim_saglayici", event_organizer: "organizasyon",
  personal_care_store: "kisisel_bakim", clothing_store: "giyim_magazasi",
};
const EN_EXPENSE_CATEGORY = {
  food_service: "yemek_hizmeti", basic_groceries: "temel_gida",
  transport_service: "ulasim_hizmeti", individual_transport: "ulasim_bireysel",
  accommodation: "konaklama", office_supplies: "ofis_sarf_malzeme",
  office_furniture: "ofis_mobilya", tech_equipment: "teknoloji_ekipman",
  software_license: "yazilim_lisans", consulting: "danismanlik",
  alcohol: "alkol", entertainment: "eglence", tobacco: "tutun_urunleri",
  gambling: "kumar", cleaning_supplies: "temizlik", personal_care: "kisisel_bakim",
  clothing: "giyim", other: "diger",
};
const EN_ANOMALY_TYPE = {
  future_dated: "gelecek_tarihli", invalid_tax_id: "gecersiz_kimlik_no",
  business_category_mismatch: "is_kolu_kategori_uyumsuzlugu",
  prohibited_category: "yasakli_kategori", limit_exceeded: "limit_asimi",
  vat_amount_mismatch: "kdv_tutari", line_total_mismatch: "satir_toplami",
  decimal_shift_high: "ondalik_kaymasi", decimal_shift_low: "dusuk_ondalik_kaymasi",
  total_mismatch: "genel_toplam", footer_mismatch: "footer_kismi",
  duplicate_upload: "mukerrer_fis_yukleme", receipt_no_collision: "fatura_no_cakismasi",
  line_subtotal_mismatch: "ara_toplam",
};
const EN_NOTE_QUALITY = {
  sufficient: "yeterli", insufficient: "yetersiz",
  manipulative: "manipulatif", ai_generated: "ai_uretimi",
};
const EN_APPROVAL_STATUS = {
  approved: "onaylandi", rejected: "onaylanmadi", review_required: "gozden_gecirilecek",
};

function enKayitMi(girdiKaydi) {
  return girdiKaydi != null && Object.prototype.hasOwnProperty.call(girdiKaydi, "record_id");
}

// "20.07.2026" -> "2026-07-20"
function enTarihToISO(ddmmyyyy) {
  const [gun, ay, yil] = ddmmyyyy.split(".");
  return `${yil}-${ay}-${gun}`;
}

// "02.06.2026 11:47" -> "2026-06-02T11:47:00+03:00"
// Saniye/saat dilimi varsayımı: zaman_damgasi_tr (alan_adlari.py) üretimde
// bunları hep ":00"/"+03:00" sabit yazıyor, geri dönüşte güvenle varsayılabilir.
function enZamanDamgasiToISO(dmyHm) {
  const [tarih, saat] = dmyHm.split(" ");
  return `${enTarihToISO(tarih)}T${saat}:00+03:00`;
}

function enGirdiyiCevir(g, kayitIdHaritasi) {
  const kayitId = kayitIdHaritasi.get(g.record_id) || g.record_id;
  return {
    kayit_id: kayitId,
    fatura_no: g.receipt_no,
    fatura_tarihi: enTarihToISO(g.date),
    yukleme_zamani: enZamanDamgasiToISO(g.upload_timestamp),
    saat: g.time || "",
    satici_vkn: g.seller_tax_id,
    satici_unvan: g.company,
    kalemler: g.items.map((it, i) => ({
      kalem_no: i + 1,
      aciklama: it.item_description,
      miktar: it.quantity,
      birim: it.unit,
      birim_fiyat: it.unit_price,
      kdv_orani: it.vat_rate,
      satir_toplam: it.line_total,
    })),
    toplam_kdv_tutari: g.total_vat_amount,
    genel_toplam: g.total_amount,
    aciklama_metni: g.user_note || "",
  };
}

function enEtiketiCevir(e, kayitIdHaritasi) {
  const kayitId = kayitIdHaritasi.get(e.record_id) || e.record_id;
  return {
    kayit_id: kayitId,
    fatura_no: e.receipt_no,
    cift_grup_id: e.duplicate_group_id || "",
    is_kolu: EN_BUSINESS_LINE[e.business_line] || e.business_line,
    harcama_kategorileri: e.expense_categories.map((k) => EN_EXPENSE_CATEGORY[k] || k),
    is_anomali: e.is_anomaly,
    anomali_turleri: e.anomaly_types.map((t) => EN_ANOMALY_TYPE[t] || t),
    aciklama_kategorisi: EN_NOTE_QUALITY[e.note_quality] || e.note_quality,
    onay_durumu: EN_APPROVAL_STATUS[e.approval_status] || e.approval_status,
  };
}

// --- Klasör yükleme -----------------------------------------------------

folderInput.addEventListener("change", async (event) => {
  const files = Array.from(event.target.files);
  const girdiFiles = files.filter((f) => f.name.endsWith("_girdi.json"));

  // kayit_id_esleme.json (fis_uret.py üretiyor, gorsel adlandirma icin -- kayit_id
  // -> record_id yonunde) zaten final_veriler_en klasorunde var; ikinci bir dosya
  // uretmek yerine burada TERS cevirip (record_id -> kayit_id) kullaniyoruz.
  const haritaFile = files.find((f) => f.name === "kayit_id_esleme.json");
  let kayitIdHaritasi = new Map();
  if (haritaFile) {
    try {
      const ham = JSON.parse(await haritaFile.text()); // kayit_id -> record_id
      kayitIdHaritasi = new Map(Object.entries(ham).map(([kayitId, recordId]) => [recordId, kayitId]));
    } catch (err) {
      console.warn("kayit_id_esleme.json okunamadı:", err);
    }
  }

  splits = {};
  let atlananKisiselBakim = 0;

  for (const girdiFile of girdiFiles) {
    const splitAdi = girdiFile.name.replace(/_girdi\.json$/, "");
    const etiketFile = files.find((f) => f.name === `${splitAdi}_etiket.json`);
    if (!etiketFile) {
      console.warn(`${splitAdi}: eşleşen _etiket.json bulunamadı, atlanıyor.`);
      continue;
    }

    let girdi, etiket;
    try {
      girdi = JSON.parse(await girdiFile.text());
      etiket = JSON.parse(await etiketFile.text());
    } catch (err) {
      console.warn(`${splitAdi} okunamadı:`, err);
      continue;
    }

    if (enKayitMi(girdi[0])) {
      if (!haritaFile) {
        console.warn(
          `${splitAdi}: kayit_id_esleme.json seçilmedi -- kayit_id yerine ` +
          "opak record_id kullanılacak (ID ile arama çalışmaz)."
        );
      }
      girdi = girdi.map((g) => enGirdiyiCevir(g, kayitIdHaritasi));
      etiket = etiket.map((e) => enEtiketiCevir(e, kayitIdHaritasi));
    }

    const etiketMap = new Map(etiket.map((e) => [e.kayit_id, e]));
    const records = new Map();
    const pairGroups = new Map();
    const turSayaci = new Map();

    for (const g of girdi) {
      const e = etiketMap.get(g.kayit_id);
      if (!e) continue;
      if (e.is_kolu === "kisisel_bakim") {
        atlananKisiselBakim++;
        continue;
      }

      const data = {
        ...g,
        is_kolu: e.is_kolu,
        is_anomali: e.is_anomali,
        anomali_turleri: e.anomali_turleri,
        aciklama_kategorisi: e.aciklama_kategorisi,
        kalemler: g.kalemler.map((k, i) => ({ ...k, harcama_kategorisi: e.harcama_kategorileri[i] })),
      };
      const rec = {
        kayitId: g.kayit_id, data, pairKey: pairKeyOf(g),
        saticiUnvan: g.satici_unvan, genelToplam: g.genel_toplam,
        anomaliTurleri: e.anomali_turleri,
      };
      records.set(rec.kayitId, rec);

      if (!pairGroups.has(rec.pairKey)) pairGroups.set(rec.pairKey, []);
      pairGroups.get(rec.pairKey).push(rec.kayitId);

      for (const t of e.anomali_turleri) turSayaci.set(t, (turSayaci.get(t) || 0) + 1);
    }

    splits[splitAdi] = { records, pairGroups, turSayaci, consistencyCache: new Map() };
  }

  renderSplitTabs();
  const splitAdlari = Object.keys(splits);
  if (splitAdlari.length) {
    const yuklenen = splitAdlari.reduce((a, s) => a + splits[s].records.size, 0);
    const toplam = yuklenen + atlananKisiselBakim;
    sourceStatus.textContent = `${splitAdlari.length} bölme, ${toplam} kayıttan ${yuklenen}'i yüklendi` +
      (atlananKisiselBakim ? ` (${atlananKisiselBakim}'i eski is_kolu=kisisel_bakim olduğu için atlandı)` : "");
    searchInput.disabled = false;
    filterToggle.disabled = false;
    setActiveSplit(splitAdlari[0]);
  } else {
    sourceStatus.textContent = "Bu klasörde eşleşen *_girdi.json / *_etiket.json çifti bulunamadı.";
  }
});

function renderSplitTabs() {
  splitTabsEl.innerHTML = Object.entries(splits).map(([ad, s]) =>
    `<button class="split-tab" data-split="${ad}">${ad}<span class="n">${s.records.size} kayıt</span></button>`
  ).join("");
  splitTabsEl.querySelectorAll(".split-tab").forEach((btn) =>
    btn.addEventListener("click", () => setActiveSplit(btn.dataset.split))
  );
}

function setActiveSplit(splitAdi) {
  activeSplit = splitAdi;
  activeAnomaliFilter.clear();
  selectedKayitId = null;
  splitTabsEl.querySelectorAll(".split-tab").forEach((btn) =>
    btn.classList.toggle("active", btn.dataset.split === splitAdi)
  );
  searchInput.value = "";
  filtrePaneliAcik = false;
  filterToggle.classList.remove("open");
  filterOptionsEl.classList.remove("open");
  renderResultList([]);
  answerEl.innerHTML = `<p class="empty-hint">Sonuçlardan bir kayıt seçin.</p>`;
}

// --- Arama ----------------------------------------------------------------

searchInput.addEventListener("input", () => {
  activeAnomaliFilter.clear();
  filterOptionsEl.querySelectorAll(".filter-option input").forEach((cb) => (cb.checked = false));
  const q = searchInput.value.trim();
  if (!q) { renderResultList([]); return; }

  const split = splits[activeSplit];
  const idAdayi = kayitIdNormalize(q);
  let eslesenler;
  if (idAdayi && split.records.has(idAdayi)) {
    eslesenler = [idAdayi];
  } else {
    const ql = q.toLocaleLowerCase("tr");
    eslesenler = Array.from(split.records.values())
      .filter((r) => r.saticiUnvan.toLocaleLowerCase("tr").includes(ql))
      .map((r) => r.kayitId);
  }
  renderResultList(eslesenler);
});

// --- Anomali türü filtresi (çoklu seçim) ------------------------------------

let filtrePaneliAcik = false;

filterToggle.addEventListener("click", () => {
  filtrePaneliAcik = !filtrePaneliAcik;
  if (filtrePaneliAcik) {
    const split = splits[activeSplit];
    const turler = Array.from(split.turSayaci.entries()).sort((a, b) => b[1] - a[1]);
    filterOptionsEl.innerHTML =
      `<div class="filter-option filter-clear" id="filter-clear-btn">Filtreyi kaldır</div>` +
      turler.map(([tur, adet]) => `
        <label class="filter-option">
          <span><input type="checkbox" data-tur="${tur}" ${activeAnomaliFilter.has(tur) ? "checked" : ""}> ${tur}</span>
          <span class="cnt">${adet}</span>
        </label>
      `).join("");

    filterOptionsEl.querySelectorAll("input[type=checkbox]").forEach((cb) =>
      cb.addEventListener("change", () => {
        if (cb.checked) activeAnomaliFilter.add(cb.dataset.tur);
        else activeAnomaliFilter.delete(cb.dataset.tur);
        searchInput.value = "";
        uygulaAnomaliFiltresi();
      })
    );
    document.getElementById("filter-clear-btn").addEventListener("click", () => {
      activeAnomaliFilter.clear();
      filterOptionsEl.querySelectorAll("input[type=checkbox]").forEach((cb) => (cb.checked = false));
      uygulaAnomaliFiltresi();
    });
  }
  filterToggle.classList.toggle("open", filtrePaneliAcik);
  filterOptionsEl.classList.toggle("open", filtrePaneliAcik);
});

function uygulaAnomaliFiltresi() {
  if (activeAnomaliFilter.size === 0) { renderResultList([]); return; }
  const split = splits[activeSplit];
  const eslesenler = Array.from(split.records.values())
    .filter((r) => r.anomaliTurleri.some((t) => activeAnomaliFilter.has(t)))
    .map((r) => r.kayitId);
  renderResultList(eslesenler);
}

// --- Sonuç listesi ----------------------------------------------------------

function renderResultList(kayitIdler) {
  const nesil = ++sonucRenderNesli;
  if (!kayitIdler.length) {
    resultListEl.innerHTML = `<p class="empty-hint">Ara ya da filtrele.</p>`;
    return;
  }
  const split = splits[activeSplit];
  resultListEl.innerHTML = kayitIdler.map((kid) => {
    const r = split.records.get(kid);
    const grup = split.pairGroups.get(r.pairKey);
    const esVar = grup && grup.length > 1;
    return `<div class="result-item" data-kayit-id="${kid}">
      <div class="result-item-top">
        <span class="id">${kid} ${esVar ? '<span class="pair-tag">eş kayıt var</span>' : ""}</span>
        <span class="consist-badge pending" data-consist="${kid}" title="Doğrulanıyor...">···</span>
      </div>
      <span class="meta">${escapeHtml(r.saticiUnvan)} · ${r.genelToplam.toLocaleString("tr-TR", {minimumFractionDigits: 2})} ₺</span>
      <div class="consist-types" data-consist-types="${kid}"></div>
    </div>`;
  }).join("");
  resultListEl.querySelectorAll(".result-item").forEach((el) =>
    el.addEventListener("click", () => selectRecord(el.dataset.kayitId))
  );

  havuzlaCalistir(kayitIdler, async (kid) => {
    const sonuc = await kayitTutarliligiGetir(kid);
    if (nesil !== sonucRenderNesli) return; // liste bu arada değişti, yazma
    guncelleConsistBadge(kid, sonuc);
  });
}

// --- Liste satırlarının otomatik tutarlılık rozeti -----------------------

async function havuzlaCalistir(ogeler, worker, esZamanli = 6) {
  let idx = 0;
  async function isci() {
    while (idx < ogeler.length) {
      const i = idx++;
      await worker(ogeler[i]);
    }
  }
  await Promise.all(Array.from({ length: Math.min(esZamanli, ogeler.length) }, isci));
}

// kalem.kdv_tutari (ve ara_toplam) JSON'a hiç export edilmiyor -- gerçek fişte de
// hiç görülmediği için KASITLI. Sonucu: kdv_tutari anomalisi (kdv hesaplanırken
// bozulur) ile satir_toplami anomalisi (kdv eklendikten SONRA bozulur) rule-based
// tespitte aynı tek gözlemi (satir_toplam beklenenden sapıyor) üretir -- ayırt
// edilemezler, ikinci bir bağımsız denklem yok (bkz. 2026-08-20 tur notu).
// DİKKAT: fonksiyon `kdv_tutari`'yi YAPISAL OLARAK asla döndürmez (services/
// validation_api.py: reconstruct edilen nesnede property tautolojik geçer, ayrıca
// _HAM_JSON_GEREKTIREN_TURLER onu açıkça filtreler) -- o yüzden bunu "ortak"
// (gerçekten eşleşen) kümesine KATMIYORUZ, ayrı bir `esdeger` alanında dürüstçe
// "etiket X, tespit Y, bunlar bilinen bir isim çakışmasıdır" diye gösteriyoruz.
function karsilastirSonuc(result) {
  const fonksiyon = new Set(result.anomaly_types);
  const etiket = new Set(result.ground_truth ? result.ground_truth.anomaly_types : []);
  const ortak = [...fonksiyon].filter((t) => etiket.has(t));
  let yalnizFonksiyon = [...fonksiyon].filter((t) => !etiket.has(t));
  let yalnizEtiket = [...etiket].filter((t) => !fonksiyon.has(t));

  let esdeger = null;
  let not = null;
  if (yalnizEtiket.includes("kdv_tutari") && yalnizFonksiyon.includes("satir_toplami")) {
    yalnizEtiket = yalnizEtiket.filter((t) => t !== "kdv_tutari");
    yalnizFonksiyon = yalnizFonksiyon.filter((t) => t !== "satir_toplami");
    esdeger = { etiket: "kdv_tutari", tespit: "satir_toplami" };
    not = "Kalem bazlı net KDV tutarı şemada yer almadığı için kural tabanlı sistem " +
      "KDV ve satır toplamı anomalisini ayrıştıramayıp varsayılan olarak satır " +
      "toplamı anomalisi kabul eder; bu beklenen bir veri modeli kısıtı olduğundan " +
      "etiket uyumsuzluğu kontrolü kaldırılmıştır.";
  }

  const tutarli = yalnizFonksiyon.length === 0 && yalnizEtiket.length === 0;
  return { fonksiyon, etiket, ortak, yalnizFonksiyon, yalnizEtiket, tutarli, esdeger, not };
}

async function kayitTutarliligiGetir(kayitId) {
  const split = splits[activeSplit];
  if (split.consistencyCache.has(kayitId)) return split.consistencyCache.get(kayitId);

  const rec = split.records.get(kayitId);
  const grup = split.pairGroups.get(rec.pairKey);
  const esKayitId = grup && grup.length > 1 ? grup.find((k) => k !== kayitId) : null;
  const esRec = esKayitId ? split.records.get(esKayitId) : null;

  let sonuc;
  try {
    const response = await fetch("/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fatura: rec.data, es_fatura: esRec ? esRec.data : null }),
    });
    if (!response.ok) throw new Error(`${response.status}`);
    const result = await response.json();
    const { tutarli, etiket, not } = karsilastirSonuc(result);
    sonuc = { durum: "ok", tutarli, turler: [...etiket], not };
  } catch (err) {
    sonuc = { durum: "hata" };
  }
  split.consistencyCache.set(kayitId, sonuc);
  return sonuc;
}

function guncelleConsistBadge(kid, sonuc) {
  const badge = resultListEl.querySelector(`[data-consist="${kid}"]`);
  const typesEl = resultListEl.querySelector(`[data-consist-types="${kid}"]`);
  if (!badge) return;

  if (sonuc.durum === "hata") {
    badge.textContent = "?";
    badge.className = "consist-badge error";
    badge.title = "Doğrulama alınamadı";
    return;
  }

  if (sonuc.tutarli) {
    badge.textContent = "✓";
    badge.className = "consist-badge ok";
    badge.title = sonuc.not ? `Tutarlı (not: ${sonuc.not})` : "Tutarlı";
    if (typesEl && sonuc.turler.length) {
      typesEl.innerHTML = sonuc.turler.map((t) => `<span class="chip mini">${escapeHtml(t)}</span>`).join("");
    }
  } else {
    badge.textContent = "✗";
    badge.className = "consist-badge bad";
    badge.title = "Tutarsızlık var";
  }
}

// --- Kayıt seçimi + doğrulama -----------------------------------------------

async function selectRecord(kayitId) {
  selectedKayitId = kayitId;
  resultListEl.querySelectorAll(".result-item").forEach((el) =>
    el.classList.toggle("selected", el.dataset.kayitId === kayitId)
  );

  const split = splits[activeSplit];
  const rec = split.records.get(kayitId);
  const grup = split.pairGroups.get(rec.pairKey);
  const esKayitId = grup && grup.length > 1 ? grup.find((k) => k !== kayitId) : null;
  const esRec = esKayitId ? split.records.get(esKayitId) : null;

  answerEl.innerHTML = `<p class="empty-hint">Kontrol ediliyor...</p>`;

  try {
    const response = await fetch("/validate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fatura: rec.data, es_fatura: esRec ? esRec.data : null }),
    });
    if (!response.ok) throw new Error(`${response.status}: ${await response.text()}`);
    const result = await response.json();
    renderAnswer(result);
    const { tutarli, etiket, not } = karsilastirSonuc(result);
    const sonuc = { durum: "ok", tutarli, turler: [...etiket], not };
    split.consistencyCache.set(kayitId, sonuc);
    guncelleConsistBadge(kayitId, sonuc);
  } catch (err) {
    answerEl.innerHTML = `<p class="empty-hint">Hata: ${escapeHtml(err.message)}</p>`;
  }
}

function renderAnswer(result) {
  const { fonksiyon, ortak, yalnizFonksiyon, yalnizEtiket, tutarli, esdeger, not } = karsilastirSonuc(result);

  let html = `<div class="result-summary">
    <span class="status-badge ${result.is_anomaly ? "anomaly" : "clean"}">${result.is_anomaly ? "Anomali var" : "Temiz"}</span>
    <span class="status-badge ${tutarli ? "consistent" : "inconsistent"}">${tutarli ? "Tutarlı" : "Tutarsızlık"}</span>
  </div>`;

  if (not) {
    html += `<div class="inconsistency-note">${escapeHtml(not)}</div>`;
  }
  if (esdeger) {
    // "Ortak" DEĞİL: fonksiyon `esdeger.etiket`'i (kdv_tutari) yapısal olarak asla
    // döndürmez (bkz. karsilastirSonuc yorumu) -- bu yalnız etiket<->tespit eşleme notu.
    html += `<div class="result-section"><h3>Eşdeğer sayılan (isim çakışması)</h3>
      <div class="chip-row">
        <span class="chip warn">${escapeHtml(esdeger.etiket)} (etiket)</span>
        <span class="chip">${escapeHtml(esdeger.tespit)} (tespit)</span>
      </div></div>`;
  }

  if (!tutarli) {
    html += `<div class="inconsistency-note">Fonksiyonun tespit ettiği ile etiketteki gerçek anomali kümesi uyuşmuyor.</div>`;
    html += chipSection("Ortak", ortak, "muted");
    html += chipSection("Yalnız fonksiyonda", yalnizFonksiyon, "");
    html += chipSection("Yalnız etikette", yalnizEtiket, "warn");
  } else {
    html += chipSection("Anomali türleri", [...fonksiyon], "");
  }

  html += `<div class="result-section"><h3>Kural ihlali detayları</h3>`;
  html += result.violation_details.length
    ? `<ul class="detail-list">${result.violation_details.map((d) => `<li>${escapeHtml(d)}</li>`).join("")}</ul>`
    : `<p class="empty-hint">Yok</p>`;
  html += `</div>`;

  answerEl.innerHTML = html;
}

function chipSection(baslik, turler, stil) {
  let h = `<div class="result-section"><h3>${baslik}</h3>`;
  h += turler.length
    ? `<div class="chip-row">${turler.map((t) => `<span class="chip ${stil}">${escapeHtml(t)}</span>`).join("")}</div>`
    : `<p class="empty-hint">Yok</p>`;
  return h + `</div>`;
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}
