# ============================================================
#  CẤU HÌNH — điền các giá trị bạn cần, KHÔNG cần điền hết.
#  Biến môi trường trên Render (Environment) luôn được ưu tiên hơn file này.
#
#  ⚠ QUAN TRỌNG: file này chứa bí mật. Nếu đưa lên GitHub thì repository PHẢI để PRIVATE.
#  Key Gemini nằm trong repo công khai sẽ bị Google quét và vô hiệu hóa trong vài phút.
# ============================================================

# --- 1) TÀI KHOẢN ADMIN (là bạn) ---------------------------------
# Đăng nhập bằng email + mật khẩu này sẽ có nút 👑 Quản trị. Mật khẩu từ 8 ký tự.
# Để trống: hệ thống tự tạo admin@huy.local và in mật khẩu trong Render > Logs.
ADMIN_EMAIL = "huymeomeoooo@gmail.com"
ADMIN_PASSWORD = "anhhuy11"

# --- 2) API KEY GEMINI -------------------------------------------
# Có thể điền nhiều key. Key đầu bị lỗi/hết hạn mức thì tự chuyển sang key kế tiếp.
# Thứ tự thử: key nhập trong ⚙ của người dùng -> các key dưới đây (và GEMINI_API_KEY trên Render) -> key dự phòng trong index.html.
# >>> DÁN KEY GEMINI CỦA BẠN VÀO ĐÂY (mỗi key một dòng, trong dấu nháy, cách nhau dấu phẩy) <<<
# Hoặc đăng nhập admin -> bấm ⚙ -> dán key vào ô "Key lưu trên máy chủ" (không cần sửa file).
GEMINI_API_KEYS = [
    
    #
    
]

# --- 3) ĐĂNG NHẬP BẰNG GOOGLE (tùy chọn) --------------------------
# Lấy tại https://console.cloud.google.com/apis/credentials -> Create credentials -> OAuth client ID -> Web application
# Authorized JavaScript origins: thêm địa chỉ website, ví dụ https://ten-ban.onrender.com
GOOGLE_CLIENT_ID = ""

# --- 4) ĐĂNG NHẬP BẰNG FACEBOOK (tùy chọn) ------------------------
# Lấy tại https://developers.facebook.com/apps -> Tạo ứng dụng -> thêm "Facebook Login for Web"
# App ID dùng ở trang web, App Secret chỉ dùng ở máy chủ (đừng đưa vào HTML).
FACEBOOK_APP_ID = ""
FACEBOOK_APP_SECRET = ""
