import { UIAbility } from '@kit.AbilityKit';
import { window } from '@kit.ArkUI';

export default class EntryAbility extends UIAbility {
  onWindowStageCreate(windowStage: window.WindowStage): void {
    // Keep system bars outside the web content; the shared page also respects safe-area insets.
    windowStage.getMainWindowSync().setWindowLayoutFullScreen(false);
    windowStage.loadContent('pages/Index');
  }
}
