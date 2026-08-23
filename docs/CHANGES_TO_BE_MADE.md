# General device automation changes

Status: implemented on 2026-08-23. Legacy single-door graphs are migrated automatically when the database opens.

- [x] Add a camera trash button to the live dashboard.
- [x] Drag a generic Trigger, Condition, or Action onto the desktop canvas, then choose its behavior.
- [x] Drag nodes from anywhere on the card except an interactive control.
- [x] Click an occupied endpoint to remove its connection; click two empty endpoints to connect them.
- [x] Give condition diamonds four usable endpoints, true/false branches, and typed Boolean/string/number values.
- [x] Save each automation's node layout and persist the dashboard's selected automation.
- [x] Replace the dashboard when its automation changes and persist its removable/reorderable camera, device, and manual modules.
- [x] Add safe-test and confirmed live-run controls when the selected automation has a manual activator.
- [x] Select an eWeLink device and channel in each device action.
- [x] Merge paired appeared/disappeared and online/offline triggers into one Boolean-configured trigger.
- [x] Remove the user-facing Primary Door role and use general device actions.
- [x] Make connection lines easier to select and configure wait/variable transition steps.
- [x] Persist the full eWeLink account inventory; a new sign-in reconciles added/removed devices.

## Original notes

add a trash can button in front of camera for easy delete (and maybe other buttons if it would get better qol)
for node adding on to the canvas let me drag the type of node first, then I add then what it is supposed to do
the node dragging isnt on the whole hitbox, only in a small area, change it to the whole area
I cannot destroy connections after they are made, add a feature that when I click on the 2 ends of the line (in both nodes the line connects, on the point they connect (circle)) it destroys the connection and vice versa for adding, basically doing the same thing as adding, only if it is occupied it deeltes the connection
the diamond shape should have 4 outputs by default, the user chooses how to use them, for example if the user want the input can go in the up point and the out on the left point and right point, the diamond should be able to have one for true and one for false. Also add type of variables to the diamond shape like "bool", "string", etc
 also each automation should have their layout, if I select for example the door automation, it should show me the dashboard for that, if I go to another automation it should show me that, that should be selected in the dashboard page itself
the action should let me choose the device I want to activate and what slot I want to turn on (like 1,2,3,4)
the object appeared, authorized target appeared, etc any thing that is either true/false and is the same concept should be the same type and in the settings of the block is where we define if it true or false (like: "authorized target appears" == true/false)
there should be not primary door there should only be a device, this program is now a way to automate and manage multiple devices, so the existence of a "main door" is stupid
the wait/other settings in the connections are impossible to configure because I cant select the connection and add settings, it should be a node to be added, you should just select the connection and add the additional step
after the first login with ewelink on the app, on the connected devices the devices associated to the account should appear permanently (until new sign in where there is new/removed devices)
again remove the concept of primary door
after this implementation think about thing that dont make sense and ask me if you should change or not
