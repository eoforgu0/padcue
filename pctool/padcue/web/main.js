// 操作画面のエントリーポイント。import は上の層が下の層を読む一方向にし(この並びは下の層から)、
// 下の層から上の層の画面を呼ぶときは core.js の links を通す。

import './core.js';
import './lists.js';
import './pick.js';
import './devtools.js';
import './devices.js';
import './manual.js';
import './frametable.js';
import './condition.js';
import './procedure.js';
import './procprops.js';
import './run.js';
import './pack.js';
import './flash.js';
import './shell.js';
