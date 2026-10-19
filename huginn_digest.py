"""huginn-digest的PyInstaller入口；源码也可直接运行。"""
if __name__ == '__main__':
    import multiprocessing
    multiprocessing.freeze_support()
    from interaction.cli import main
    raise SystemExit(main())
