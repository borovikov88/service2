INDEXABLE_HOSTS = {
    "rovikpool.ru",
    "www.rovikpool.ru",
}


def is_indexable_host(host):
    return host in INDEXABLE_HOSTS
