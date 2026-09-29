# Источники и зависимости

Программные API сверялись с первичной документацией. Собственная реализация не копирует планы,
условные графические знаки из ГОСТов или геометрию реальных проектов. Demo построено программно
из явно заданных прямоугольников и отрезков.

- CPython / tkinter: https://docs.python.org/3.12/library/tkinter.html
- Tk canvas, Tcl/Tk: https://www.tcl-lang.org/man/tcl8.6/TkCmd/canvas.htm
- ezdxf (Drawing): https://ezdxf.readthedocs.io/en/stable/drawing/drawing.html
- ezdxf (DXF tags): https://ezdxf.readthedocs.io/en/stable/dxfinternals/dxftags.html
- ezdxf (Path): https://ezdxf.readthedocs.io/en/stable/path.html

`data/plants_index.json` получен только из массива plants файла GreenCAD_registry_v1.json:
329 строк, ID не переименованы, дубли/формы сортов не объединены. SHA256 исходного JSON в самом индексе.

`data/site_types.json` получен только из object_types файла GreenCAD_site_dictionary_v1.json:
140 ID и подписей. Значение наличия ID — принадлежность словарю, а не реализованная поддержка всех
физических особенностей этого объекта. SHA256 исходника в индексе.

Ни одно правило из ГОСТ/СП здесь не включено. Никакая новая величина из каталога не объявлена
установленной, если её не было в исходных материалах. Радиусы в project_types.json — пользовательские
графические настройки прототипа. Исходные книги и JSON-реестры не изменялись.

Лицензии: CPython — PSF, Tcl/Tk — собственная BSD-подобная, ezdxf — MIT, NumPy — BSD,
pyparsing — MIT, typing_extensions — PSF, fontTools — MIT. Зависимости не вложены бинарно в ZIP;
они устанавливаются отдельно с сохранением собственных лицензий. Шрифты не поставляются.

Ранее рассматривался PySide6. Для текущего узкого локального редактора использован Tkinter/ttk.
Классы координат, команд, сохранения и экспорта не зависят от выбора GUI.
