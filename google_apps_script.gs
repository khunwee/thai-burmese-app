/**
 * วุ้นแปลภาษาพม่า V15 — ตัวแปลภาษาส่วนตัวบน Google Apps Script (ฟรี)
 *
 * ใช้ Google Translate ผ่านบัญชี Google ของคุณเอง (LanguageApp)
 *  - ฟรี 5,000 ครั้ง/วัน (บัญชี Gmail) · 20,000 ครั้ง/วัน (Google Workspace)
 *  - ทำงานบนเซิร์ฟเวอร์ของ Google จึงไม่โดนจำกัดเพราะ IP ร่วมของ Render
 *
 * วิธีติดตั้ง (ดูรายละเอียดใน README หัวข้อ 4):
 *  1. ไปที่ https://script.google.com → New project → วางโค้ดนี้ทั้งหมด
 *  2. เปลี่ยน SECRET ด้านล่างเป็นข้อความสุ่มยาว ๆ (แล้วใส่ค่าเดียวกันใน Render: GAS_SECRET)
 *  3. Deploy → New deployment → Type: Web app
 *       Execute as: Me   ·   Who has access: Anyone
 *  4. คัดลอก Web app URL (ลงท้าย /exec) ไปใส่ใน Render: GAS_URL
 */
const SECRET = 'CHANGE-ME-to-a-long-random-text';

function doPost(e) {
  try {
    const req = JSON.parse(e.postData.contents);
    if (req.secret !== SECRET) return json_({ ok: false, error: 'unauthorized' });
    const source = req.source || 'th';
    const target = req.target || 'my';
    const texts = Array.isArray(req.texts) ? req.texts.slice(0, 20) : [];
    const translations = texts.map(function (t) {
      return t ? LanguageApp.translate(String(t), source, target) : '';
    });
    return json_({ ok: true, translations: translations });
  } catch (err) {
    return json_({ ok: false, error: String(err) });
  }
}

// เปิด URL ในเบราว์เซอร์เพื่อเช็กว่า Deploy สำเร็จ (ควรเห็น ok:true และคำแปลตัวอย่าง)
function doGet() {
  return json_({ ok: true, service: 'thai-burmese-translate', sample: LanguageApp.translate('สวัสดีครับ', 'th', 'my') });
}

function json_(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}
