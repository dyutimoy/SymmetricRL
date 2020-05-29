#!/bin/bash




pip download pybullet
tar -xzf pybullet*.tar.gz
cd $(find -type d -iname "pybullet-*")
sed -i -- 's/2 \* multiprocessing.cpu_count()/4/g' setup.py
python setup.py install
cd ..
rm -r pybullet-*
