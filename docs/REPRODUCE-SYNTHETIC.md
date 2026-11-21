# 合成数据复现说明

```bash
cd closed-loop-public
python tools/build_synthetic_dataset.py
python -m unittest discover -s tests
python tools/philosophy_check.py
```

预期：8 期合成数据生成成功，79 项校验全部通过，单元测试通过。

如真实数据缺失，本包不需要真实数据；所有实验均可在合成数据上复跑。
