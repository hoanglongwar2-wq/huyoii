# Nguyễn Anh Huy AI — bản có tài khoản

Website trợ lý AI giao diện tối, dùng Google Gemini. API key chỉ được đọc ở backend (biến môi trường) hoặc do người dùng tự dán trong ⚙ Cài đặt.

## Có gì mới ở bản này

- **Đăng ký / đăng nhập / đăng xuất / đổi mật khẩu.** Mật khẩu băm bằng scrypt, cookie phiên HttpOnly, tự khóa 15 phút nếu nhập sai 8 lần.
- **Tài khoản admin = bạn.** Đặt `ADMIN_EMAIL` + `ADMIN_PASSWORD` trong biến môi trường. Admin có nút 🛡 để xem thống kê, khóa/mở khóa, đặt lại mật khẩu, xóa người dùng. Admin không đọc được nội dung chat của người khác. Email admin không thể bị người khác đăng ký.
- **Chat lưu trên máy chủ theo tài khoản**, đăng nhập thiết bị nào cũng thấy. Có nút ✕ để xóa chat (xóa luôn file trong đó). Lịch sử cũ trong trình duyệt được hỏi nhập vào lần đăng nhập đầu tiên.
- **Tạo code mọi ngôn ngữ** (Python, JS/TS, HTML/CSS, Java, C/C++, C#, Go, Rust, PHP, SQL, shell, Docker, YAML, JSON…). Một file thì gửi file, nhiều file thì tự nén `.zip`. Tối đa **50 MB** mỗi lần.
- **File lưu trong chat để sửa lại.** Mỗi lần AI tạo xong sẽ gửi file kèm nút tải, file được lưu cùng cuộc trò chuyện. Nếu code chạy lỗi, mở lại chat đó, dán lỗi vào, AI nhận lại đúng phiên bản mới nhất của file, sửa và gửi bản mới. Mở lại chat có file thì chế độ **💻 Tạo code** tự bật. Nút **🛠 Sửa code** cho phép tải lên một file có sẵn để AI sửa.
- Ảnh tạo ra cũng được lưu trong chat.

## Biến môi trường

| Biến | Ý nghĩa |
|---|---|
| `GEMINI_API_KEY` | Key Gemini của máy chủ (hoặc người dùng tự dán trong ⚙) |
| `ADMIN_EMAIL`, `ADMIN_PASSWORD` | Tài khoản admin (mật khẩu ≥ 8 ký tự). Đổi biến = đổi mật khẩu admin |
| `SECRET_KEY` | Chuỗi ngẫu nhiên dài để ký phiên đăng nhập (Render tự sinh qua `render.yaml`) |
| `DB_PATH` | Đường dẫn file SQLite, mặc định `data/huy_ai.db` |
| `USER_QUOTA_MB` | Dung lượng file tối đa mỗi tài khoản (mặc định 300) |
| `ALLOW_REGISTER` | `0` để tắt đăng ký tự do (chỉ admin hiện có, người cũ vẫn dùng) |
| `GEMINI_MODEL`, `GEMINI_IMAGE_MODEL`, `GEMINI_FALLBACK_MODELS`, `SYSTEM_INSTRUCTION` | Như bản trước |
| `CORS_ORIGINS` | Chỉ cần khi frontend nằm ở domain khác (bật cookie chéo domain) |

## Chạy trên máy tính

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: py -m venv .venv ; .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env     # điền GEMINI_API_KEY, ADMIN_EMAIL, ADMIN_PASSWORD
python backend.py        # mở http://127.0.0.1:5000
```

## Đưa lên Render (GitHub + Blueprint)

1. Upload toàn bộ file lên **thư mục gốc** repository GitHub (không upload `.env`, thư mục `data/`).
2. Render → New → Blueprint → chọn repo. Điền khi được hỏi: `GEMINI_API_KEY`, `ADMIN_EMAIL`, `ADMIN_PASSWORD` (`SECRET_KEY` tự sinh).
3. Mở `https://...onrender.com`, đăng nhập bằng email + mật khẩu admin đã đặt.

## ⚠ Lưu trữ dữ liệu trên Render Free

Tài khoản, chat và file nằm trong một file SQLite. Gói **Free của Render dùng đĩa tạm**: mỗi lần deploy hoặc khi máy ngủ rồi thức dậy, file này bị xóa, nghĩa là **người dùng thường và chat sẽ mất** (tài khoản admin vẫn được tạo lại từ biến môi trường). Muốn lưu lâu dài:

- Gắn **Disk** của Render (gói trả phí) tại `/var/data`, đặt `DB_PATH=/var/data/huy_ai.db`; hoặc
- Chạy trên máy/VPS của bạn; hoặc
- Chuyển sang database ngoài (Postgres) — cần sửa lớp truy cập dữ liệu trong `backend.py` (các hàm `q`, `q1`, `ex`).

## Giới hạn

- Code sinh ra phụ thuộc giới hạn đầu ra của model: file thật sự rất dài có thể bị cắt (AI tự viết tiếp tối đa 6 lần, sẽ có cảnh báo). Mốc 50 MB là trần của máy chủ, không phải độ dài thực tế model viết được.
- Chỉ tạo được file dạng văn bản/code (không tạo .docx, .xlsx, ảnh nhị phân…).
- Backend tự kiểm tra cú pháp `.py` và `.json`; ngôn ngữ khác thì chạy thử rồi dán lỗi vào chat để AI sửa.
- Đính kèm ảnh/PDF của người dùng không được lưu lại (chỉ lưu tên file). Ảnh AI tạo và file code thì được lưu.
- File đính kèm tối đa 10 MB, PDF 35 trang; request tối đa 40 MB.
- Tải video/MP3 (YouTube, TikTok) giữ nguyên như cũ, nay yêu cầu đăng nhập.
- Không nhập mật khẩu, thông tin ngân hàng hay dữ liệu nhạy cảm vào chatbot.

## Cấu trúc

```text
backend.py          # Flask: tài khoản, admin, chat, file, tạo code
static/index.html   # giao diện
requirements.txt  render.yaml  Procfile  .env.example  .gitignore
```


## Cập nhật v7: admin, đăng nhập Google/Facebook, key dự phòng

Mở file `config.py` (cạnh `backend.py`) và điền:

- `ADMIN_EMAIL`, `ADMIN_PASSWORD`: tài khoản admin của bạn (mật khẩu từ 8 ký tự). Để trống thì hệ thống tự tạo `admin@huy.local` và in mật khẩu trong Render > Logs.
- `GEMINI_API_KEYS`: danh sách key. Key lỗi hoặc hết hạn mức sẽ tự chuyển sang key kế tiếp. Thứ tự: key nhập ở ⚙ → key trong `config.py`/Render → key dự phòng trong `static/index.html` (`BUILTIN_API_KEYS`).
- `GOOGLE_CLIENT_ID`, `FACEBOOK_APP_ID`, `FACEBOOK_APP_SECRET`: để bật nút đăng nhập Google/Facebook. Chưa điền thì nút tự ẩn.

Mọi giá trị trên cũng đặt được bằng biến môi trường trên Render (ưu tiên hơn `config.py`).
Nếu đặt key hoặc mật khẩu trong `config.py` thì repository GitHub phải để **Private**.
