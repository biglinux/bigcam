# Maintainer: Barnabé di Kartola <barnabedikartola@gmail.com>

pkgname=biglinux-package-template
pkgdesc="Package template for biglinux"
# depends=('')
# makedepends=('')
# conflicts=('')
pkgver=$(date +%y.%m.%d)
pkgrel=$(date +%H%M)
arch=('any')
license=('GPL3')
url="https://github.com/biglinux/$pkgname"
provides=("$pkgname")
source=("git+${url}.git")
md5sums=('SKIP')
if [ -e "${pkgname}.install" ];then
    install=${pkgname}.install
elif [ -e "pkgbuild.install" ];then
    install=pkgbuild.install
fi

package() {
    # Verify default folder
    if [ -d "${srcdir}/${pkgname}/${pkgname}" ]; then
        InternalDir="${srcdir}/${pkgname}/${pkgname}"
    else
        InternalDir="${srcdir}/${pkgname}"
    fi

    # Copy files
    if [ -d "${InternalDir}/usr" ]; then
        cp -r "${InternalDir}/usr" "${pkgdir}/"
    fi

    if [ -d "${InternalDir}/etc" ]; then
        cp -r "${InternalDir}/etc" "${pkgdir}/"
    fi

    if [ -d "${InternalDir}/opt" ]; then
        cp -r "${InternalDir}/opt" "${pkgdir}/"
    fi
}
