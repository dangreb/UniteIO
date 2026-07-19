pip uninstall -y dist\UniteIO-0.1-py3-none-any.whl
python setup.py sdist bdist_wheel
pip install dist\UniteIO-0.1-py3-none-any.whl