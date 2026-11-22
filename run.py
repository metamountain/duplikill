"""Entry point for the exe (PyInstaller) and for starting by hand."""
import multiprocessing

if __name__ == "__main__":
    multiprocessing.freeze_support()
    from duplikill.app import main
    main()
