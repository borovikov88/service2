def brand_context(request):
    host = request.get_host().split(":", 1)[0].lower()

    default_brand = {
        "name": "RovikPool",
        "logo": "pool_service/img/logo.png",
        "favicon": "assets/images/favicon.png",
    }
    brands_by_host = {
        "rovikpool.ru": default_brand,
        "www.rovikpool.ru": default_brand,
        "service2.aqualine22.ru": {
            "name": "\u0410\u043a\u0432\u0430\u043b\u0430\u0439\u043d",
            "logo": "assets/images/aqualine.png",
            "favicon": "assets/images/aqualine.png",
        },
        "www.service2.aqualine22.ru": {
            "name": "\u0410\u043a\u0432\u0430\u043b\u0430\u0439\u043d",
            "logo": "assets/images/aqualine.png",
            "favicon": "assets/images/aqualine.png",
        },
    }

    brand = brands_by_host.get(host, default_brand)
    return {
        "brand_name": brand["name"],
        "brand_logo": brand["logo"],
        "brand_favicon": brand["favicon"],
    }
