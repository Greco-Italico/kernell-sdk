#!/bin/bash
set -e

if [ "$EUID" -ne 0 ]; then
  echo "Por favor, corre este script con sudo: sudo ./build_rootfs.sh"
  exit 1
fi

echo "[+] Creando entorno de compilación de rootfs..."
mkdir -p build_rootfs
cd build_rootfs

if [ ! -f "alpine-minirootfs-3.19.0-x86_64.tar.gz" ]; then
    wget https://dl-cdn.alpinelinux.org/alpine/latest-stable/releases/x86_64/alpine-minirootfs-3.19.0-x86_64.tar.gz
fi

mkdir -p rootfs
tar -xzf alpine-minirootfs-3.19.0-x86_64.tar.gz -C rootfs/

echo "[+] Configurando red local para apk..."
cp /etc/resolv.conf rootfs/etc/

echo "[+] Instalando Python 3..."
chroot rootfs /bin/sh -c "apk update && apk add python3"

echo "[+] Copiando runner y script de init..."
mkdir -p rootfs/app
cp ../runner.py rootfs/app/
cp ../init rootfs/
chmod +x rootfs/init
chmod -R 755 rootfs/app

echo "[+] Creando imagen ext4 (rootfs.img)..."
dd if=/dev/zero of=../rootfs.img bs=1M count=128
mkfs.ext4 ../rootfs.img

echo "[+] Montando imagen y copiando archivos..."
mkdir -p mnt
mount ../rootfs.img mnt
cp -r rootfs/* mnt/
umount mnt

echo "[+] Limpiando..."
cd ..
rm -rf build_rootfs

echo "[+] Listo. Imagen generada: rootfs.img"
