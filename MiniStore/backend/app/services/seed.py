"""Idempotent seed data for FinShop LIVE (14 Vietnamese test products, vouchers, LIVE).

Invariant: Never resets or deletes existing customer orders on application restart.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from app.models.entities import (
    CommerceSession,
    Customer,
    Inventory,
    LiveProduct,
    LiveSession,
    Merchant,
    Product,
    ProductVariant,
    Voucher,
)
from app.models.enums import LiveSessionStatus, VoucherDiscountType

DEFAULT_MERCHANT_ID = "MERCHANT_FINSHOP_01"
DEFAULT_SESSION_ID = "CSESS_FINSHOP_DEFAULT_01"
DEFAULT_LIVE_SESSION_ID = "LIVE_FINSHOP_01"
DEFAULT_CUSTOMER_1_ID = "CUST_MINH_01"
DEFAULT_CUSTOMER_1_TOKEN = "ctk_finshop_minh_test_01"  # noqa: S105
DEFAULT_CUSTOMER_2_ID = "CUST_LINH_02"
DEFAULT_CUSTOMER_2_TOKEN = "ctk_finshop_linh_test_02"  # noqa: S105

SEED_PRODUCTS: tuple[dict[str, Any], ...] = (
    # 1. Thời trang (Fashion)
    {
        "id": "PROD-FASH-01",
        "slug": "ao-khoac-gio-chong-nang-uv-finshop",
        "title": "Áo khoác gió chống nắng UV 50+ siêu nhẹ FinShop",
        "category": "Thời trang",
        "description": (
            "[Dữ liệu thử nghiệm] Chất vải dù trượt nước thoáng khí, chống tia UV, "
            "phù hợp mặc đi làm và di chuyển hàng ngày."
        ),
        "images": ["/products/fash-jacket.svg"],
        "is_featured": True,
        "variants": [
            ("VAR-FASH-01-M", "FS-UVJ-KEM-M", "Kem Sữa - Size M", 249000, 350000, 18),
            ("VAR-FASH-01-L", "FS-UVJ-DEN-L", "Đen Than - Size L", 249000, 350000, 12),
        ],
    },
    {
        "id": "PROD-FASH-02",
        "slug": "ao-thun-cotton-compact-250gsm",
        "title": "Áo thun Cotton Compact 250gsm Form Boxy",
        "category": "Thời trang",
        "description": (
            "[Dữ liệu thử nghiệm] Cotton 100% dày dặn đứng form, không xù lông sau nhiều lần giặt."
        ),
        "images": ["/products/fash-tshirt.svg"],
        "is_featured": True,
        "variants": [
            ("VAR-FASH-02-TRANG", "FS-TEE-WHT-L", "Trắng Tinh - Size L", 169000, 229000, 35),
            ("VAR-FASH-02-XAM", "FS-TEE-GRY-M", "Xám Khói - Size M", 169000, 229000, 24),
        ],
    },
    {
        "id": "PROD-FASH-03",
        "slug": "tui-tote-canvas-chong-tham",
        "title": "Túi Tote Canvas chống thấm đựng Laptop 15 inch",
        "category": "Thời trang",
        "description": (
            "[Dữ liệu thử nghiệm] Thiết kế tối giản, có khóa kéo và ngăn chống sốc tiện dụng."
        ),
        "images": ["/products/fash-tote.svg"],
        "is_featured": False,
        "variants": [
            ("VAR-FASH-03-BE", "FS-TOTE-BE", "Màu Be Tự Nhiên", 139000, 199000, 22),
        ],
    },
    # 2. Phụ kiện điện tử (Electronics accessories)
    {
        "id": "PROD-TECH-01",
        "slug": "tai-nghe-bluetooth-anc-finpods-pro",
        "title": "Tai nghe Bluetooth chống ồn chủ động FinPods Pro",
        "category": "Phụ kiện điện tử",
        "description": (
            "[Dữ liệu thử nghiệm] Pin 28 giờ kèm hộp sạc USB-C, chống ồn ANC 38dB, "
            "độ trễ thấp khi xem LIVE và họp trực tuyến."
        ),
        "images": ["/products/tech-earbuds.svg"],
        "is_featured": True,
        "variants": [
            ("VAR-TECH-01-WHT", "FS-PODS-WHT", "Trắng Ngọc", 490000, 690000, 6),
            ("VAR-TECH-01-BLK", "FS-PODS-BLK", "Đen Nhám", 490000, 690000, 4),
        ],
    },
    {
        "id": "PROD-TECH-02",
        "slug": "sac-du-phong-magsafe-10000mah-20w",
        "title": "Sạc dự phòng nam châm 10.000mAh sạc nhanh PD 20W",
        "category": "Phụ kiện điện tử",
        "description": (
            "[Dữ liệu thử nghiệm] Thiết kế mỏng nhẹ, tích hợp giá đỡ gập tiện lợi khi xem video."
        ),
        "images": ["/products/tech-powerbank.svg"],
        "is_featured": True,
        "variants": [
            ("VAR-TECH-02-TITAN", "FS-PB-10K-TI", "Xám Titan 10.000mAh", 359000, 490000, 15),
        ],
    },
    {
        "id": "PROD-TECH-03",
        "slug": "cap-sac-nhanh-usb-c-65w-boc-du",
        "title": "Cáp sạc nhanh USB-C 65W bọc dù siêu bền 1.5m",
        "category": "Phụ kiện điện tử",
        "description": (
            "[Dữ liệu thử nghiệm] Hỗ trợ sạc nhanh cho điện thoại, máy tính bảng và laptop USB-C."
        ),
        "images": ["/products/tech-cable.svg"],
        "is_featured": False,
        "variants": [
            ("VAR-TECH-03-15M", "FS-CBL-65W-15", "Đen 1.5m (65W)", 79000, 119000, 60),
        ],
    },
    # 3. Mỹ phẩm (Cosmetics)
    {
        "id": "PROD-COSM-01",
        "slug": "serum-b5-phuc-hoi-cap-am-30ml",
        "title": "Serum Vitamin B5 + HA cấp ẩm phục hồi da 30ml",
        "category": "Mỹ phẩm",
        "description": ("[Dữ liệu thử nghiệm] Kết cấu thấm nhanh không bết dính, dịu da nhạy cảm."),
        "images": ["/products/cosm-serum.svg"],
        "is_featured": True,
        "variants": [
            ("VAR-COSM-01-30ML", "FS-SERUM-B5-30", "Chai 30ml Tiêu chuẩn", 265000, 340000, 14),
            ("VAR-COSM-01-50ML", "FS-SERUM-B5-50", "Chai 50ml Tiết kiệm", 385000, 490000, 8),
        ],
    },
    {
        "id": "PROD-COSM-02",
        "slug": "kem-chong-nang-nang-tone-spf50-50ml",
        "title": "Kem chống nắng kiềm dầu nâng tone nhẹ SPF50+ PA++++",
        "category": "Mỹ phẩm",
        "description": (
            "[Dữ liệu thử nghiệm] Không cay mắt, không vệt trắng, kiềm dầu nhẹ đến 8 giờ."
        ),
        "images": ["/products/cosm-sunscreen.svg"],
        "is_featured": True,
        "variants": [
            ("VAR-COSM-02-50ML", "FS-SUN-50ML", "Tuýp 50ml", 219000, 295000, 20),
        ],
    },
    {
        "id": "PROD-COSM-03",
        "slug": "son-kem-li-nhung-mem-moi",
        "title": "Son kem lì nhung mịn môi lâu trôi FinVelvet",
        "category": "Mỹ phẩm",
        "description": (
            "[Dữ liệu thử nghiệm] Chất son mềm mượt không khô môi, sắc đỏ đất thời thượng."
        ),
        "images": ["/products/cosm-lipstick.svg"],
        "is_featured": False,
        "variants": [
            ("VAR-COSM-03-01", "FS-LIP-01-DD", "01 Đỏ Đất", 145000, 195000, 28),
            ("VAR-COSM-03-02", "FS-LIP-02-CD", "02 Cam Đào", 145000, 195000, 19),
        ],
    },
    # 4. Gia dụng (Household products)
    {
        "id": "PROD-HOME-01",
        "slug": "binh-giu-nhiet-inox-316-900ml",
        "title": "Bình giữ nhiệt Inox 316 dung tích 900ml kèm ống hút",
        "category": "Gia dụng",
        "description": (
            "[Dữ liệu thử nghiệm] Giữ lạnh 18 giờ, giữ nóng 10 giờ, sơn tĩnh điện chống trầy."
        ),
        "images": ["/products/home-tumbler.svg"],
        "is_featured": True,
        "variants": [
            ("VAR-HOME-01-MINT", "FS-TUMB-900-MN", "Xanh Mint 900ml", 189000, 260000, 10),
            ("VAR-HOME-01-ROSE", "FS-TUMB-900-RS", "Hồng Phấn 900ml", 189000, 260000, 5),
        ],
    },
    {
        "id": "PROD-HOME-02",
        "slug": "may-xay-sinh-to-cam-tay-6-luoi",
        "title": "Máy xay sinh tố cầm tay 6 lưỡi sạc USB 450ml",
        "category": "Gia dụng",
        "description": (
            "[Dữ liệu thử nghiệm] Xay nhuyễn đá nhỏ và trái cây trong 30 giây, dễ vệ sinh."
        ),
        "images": ["/products/home-blender.svg"],
        "is_featured": False,
        "variants": [
            ("VAR-HOME-02-WHT", "FS-BLND-450-W", "Trắng Kem 450ml", 279000, 390000, 9),
        ],
    },
    {
        "id": "PROD-HOME-03",
        "slug": "den-ban-hoc-chong-can-cam-ung",
        "title": "Đèn bàn LED chống cận cảm ứng 3 chế độ màu",
        "category": "Gia dụng",
        "description": (
            "[Dữ liệu thử nghiệm] Ánh sáng chuẩn Ra>90 không nhấp nháy, "
            "gập gọn tiết kiệm diện tích."
        ),
        "images": ["/products/home-lamp.svg"],
        "is_featured": False,
        "variants": [
            ("VAR-HOME-03-WHT", "FS-LAMP-LED-W", "Trắng Tối Giản", 199000, 275000, 16),
        ],
    },
    # 5. Thực phẩm & Đồ uống (Food and beverages)
    {
        "id": "PROD-FOOD-01",
        "slug": "combo-ca-phe-sua-da-rang-xay-hoa-tan",
        "title": "Combo 2 hộp Cà phê Sữa Đá Sài Gòn rang xay đậm vị",
        "category": "Thực phẩm & Đồ uống",
        "description": (
            "[Dữ liệu thử nghiệm] Đậm đà hương Robusta Buôn Ma Thuột, pha nhanh trong 1 phút."
        ),
        "images": ["/products/food-coffee.svg"],
        "is_featured": True,
        "variants": [
            ("VAR-FOOD-01-C2", "FS-COF-COMBO2", "Combo 2 hộp (24 gói)", 129000, 165000, 30),
        ],
    },
    {
        "id": "PROD-FOOD-02",
        "slug": "hat-dieu-rang-cui-binh-phuoc-500g",
        "title": "Hạt điều rang củi vỏ lụa Bình Phước hộp 500g loại A+",
        "category": "Thực phẩm & Đồ uống",
        "description": (
            "[Dữ liệu thử nghiệm] Hạt to giòn béo tự nhiên, đóng hộp hút chân không sạch sẽ."
        ),
        "images": ["/products/food-cashew.svg"],
        "is_featured": False,
        "variants": [
            ("VAR-FOOD-02-500G", "FS-CSH-500G", "Hộp 500g Loại A+", 175000, 225000, 3),
        ],
    },
)


def seed_commerce_data(db: Session) -> dict[str, Any]:
    """Idempotently seed FinShop LIVE catalog, vouchers, live session, and test identities.

    Never deletes or overwrites existing customer orders or payments.
    """
    now = datetime.now(UTC)

    merchant = db.get(Merchant, DEFAULT_MERCHANT_ID)
    if merchant is None:
        merchant = Merchant(
            id=DEFAULT_MERCHANT_ID,
            slug="finshop-official",
            name="FinShop Official Store (Test)",
            description="Gian hàng chính hãng thử nghiệm FinShop LIVE — dữ liệu mô phỏng MOCK.",
            avatar_url="/products/store-avatar.svg",
            is_active=True,
            is_test_data=True,
        )
        db.add(merchant)
        db.flush()

    csess = db.get(CommerceSession, DEFAULT_SESSION_ID)
    if csess is None:
        csess = CommerceSession(
            id=DEFAULT_SESSION_ID,
            merchant_id=DEFAULT_MERCHANT_ID,
            world_epoch=1,
            source_mode="MOCK",
            is_active=True,
        )
        db.add(csess)
    db.flush()

    for cid, tok, name, phone, addr in (
        (
            DEFAULT_CUSTOMER_1_ID,
            DEFAULT_CUSTOMER_1_TOKEN,
            "Nguyễn Minh Anh (Khách Test 1)",
            "0901234567",
            "123 Nguyễn Huệ, Quận 1, TP. Hồ Chí Minh",
        ),
        (
            DEFAULT_CUSTOMER_2_ID,
            DEFAULT_CUSTOMER_2_TOKEN,
            "Trần Khánh Linh (Khách Test 2)",
            "0909876543",
            "45 Lê Lợi, Quận Hải Châu, Đà Nẵng",
        ),
    ):
        if db.get(Customer, cid) is None:
            db.add(
                Customer(
                    id=cid,
                    merchant_id=DEFAULT_MERCHANT_ID,
                    commerce_session_id=DEFAULT_SESSION_ID,
                    display_name=name,
                    phone=phone,
                    email=f"{cid.lower()}@finshop.test",
                    default_address=addr,
                    session_token=tok,
                    is_test_identity=True,
                )
            )
    db.flush()

    seeded_products = 0
    for item in SEED_PRODUCTS:
        if db.get(Product, item["id"]) is None:
            db.add(
                Product(
                    id=item["id"],
                    merchant_id=DEFAULT_MERCHANT_ID,
                    slug=item["slug"],
                    title=item["title"],
                    category=item["category"],
                    description=item["description"],
                    image_urls_json=list(item["images"]),
                    is_featured=bool(item["is_featured"]),
                    is_active=True,
                    is_test_data=True,
                )
            )
            db.flush()
            seeded_products += 1

        for vid, sku, vtitle, price, cmp_price, stock in item["variants"]:
            if db.get(ProductVariant, vid) is None:
                db.add(
                    ProductVariant(
                        id=vid,
                        product_id=item["id"],
                        merchant_id=DEFAULT_MERCHANT_ID,
                        sku=sku,
                        title=vtitle,
                        price_vnd=price,
                        compare_at_price_vnd=cmp_price,
                        image_url=item["images"][0],
                        is_active=True,
                    )
                )
                db.flush()
                db.add(
                    Inventory(
                        id=f"INV-{vid}",
                        variant_id=vid,
                        merchant_id=DEFAULT_MERCHANT_ID,
                        on_hand_qty=stock,
                        reserved_qty=0,
                        low_stock_threshold=5,
                        version=1,
                    )
                )
                db.flush()

    # Seed valid test vouchers
    vouchers_spec = (
        (
            "VCH-FINSHOP30K",
            "FINSHOP30K",
            "Giảm 30.000đ cho đơn từ 200.000đ",
            VoucherDiscountType.FIXED_VND.value,
            30000,
            None,
            200000,
            100,
            3,
        ),
        (
            "VCH-LIVE15PCT",
            "LIVE15",
            "Giảm 15% tối đa 80.000đ phiên LIVE",
            VoucherDiscountType.PERCENT.value,
            15,
            80000,
            150000,
            50,
            2,
        ),
        (
            "VCH-FREESHIP",
            "FREESHIP25K",
            "Giảm 25.000đ phí vận chuyển cho đơn từ 120.000đ",
            VoucherDiscountType.FIXED_VND.value,
            25000,
            None,
            120000,
            200,
            5,
        ),
        (
            "VCH-FLASH50K",
            "FLASH50K",
            "Giảm 50.000đ cho đơn từ 400.000đ (Giới hạn 5 lượt)",
            VoucherDiscountType.FIXED_VND.value,
            50000,
            None,
            400000,
            5,
            1,
        ),
    )
    for vid, code, title, dtype, dval, max_d, min_o, ulimit, plimit in vouchers_spec:
        if db.get(Voucher, vid) is None:
            db.add(
                Voucher(
                    id=vid,
                    merchant_id=DEFAULT_MERCHANT_ID,
                    code=code,
                    title=title,
                    discount_type=dtype,
                    discount_value=dval,
                    max_discount_vnd=max_d,
                    min_order_vnd=min_o,
                    usage_limit=ulimit,
                    used_count=0,
                    per_customer_limit=plimit,
                    starts_at=now - timedelta(hours=1),
                    ends_at=now + timedelta(days=30),
                    is_active=True,
                )
            )
    db.flush()

    # Seed 1 configurable Live Shopping session with Flash Sale items & pinned product
    live = db.get(LiveSession, DEFAULT_LIVE_SESSION_ID)
    if live is None:
        live = LiveSession(
            id=DEFAULT_LIVE_SESSION_ID,
            merchant_id=DEFAULT_MERCHANT_ID,
            commerce_session_id=DEFAULT_SESSION_ID,
            title="Siêu Hội Săn Deal FinShop LIVE — Flash Sale Giờ Vàng",
            host_name="MC Thảo Vy · FinShop Official",
            status=LiveSessionStatus.LIVE.value,
            is_simulated_stream=True,
            media_label_vi=(
                "Phiên LIVE mô phỏng (Simulated Live Commerce) — Không phải livestream thật"
            ),
            starts_at=now - timedelta(minutes=10),
            ends_at=now + timedelta(hours=6),
        )
        db.add(live)
        db.flush()

        live_items = (
            ("LP-01", "PROD-TECH-01", "VAR-TECH-01-WHT", 1, True, 399000, 4),
            ("LP-02", "PROD-COSM-01", "VAR-COSM-01-30ML", 2, False, 215000, 8),
            ("LP-03", "PROD-FASH-01", "VAR-FASH-01-M", 3, False, 199000, 10),
            ("LP-04", "PROD-HOME-01", "VAR-HOME-01-MINT", 4, False, 149000, 6),
            ("LP-05", "PROD-FOOD-02", "VAR-FOOD-02-500G", 5, False, 139000, 2),
        )
        for lpid, pid, vid, sorder, pinned, fs_price, fs_limit in live_items:
            db.add(
                LiveProduct(
                    id=lpid,
                    live_session_id=DEFAULT_LIVE_SESSION_ID,
                    product_id=pid,
                    variant_id=vid,
                    sort_order=sorder,
                    is_pinned=pinned,
                    flash_sale_price_vnd=fs_price,
                    flash_sale_stock_limit=fs_limit,
                    flash_sale_sold_qty=0,
                    flash_sale_starts_at=now - timedelta(minutes=10),
                    flash_sale_ends_at=now + timedelta(hours=3),
                )
            )
    db.commit()
    return {
        "merchant_id": DEFAULT_MERCHANT_ID,
        "commerce_session_id": DEFAULT_SESSION_ID,
        "live_session_id": DEFAULT_LIVE_SESSION_ID,
        "newly_seeded_products": seeded_products,
    }
