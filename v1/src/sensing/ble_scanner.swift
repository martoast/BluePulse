import Foundation
import CoreBluetooth

// BLE scanner — outputs JSON lines for each discovered device.
// Throttled to max 1 output per device per second.
// Detects vendor from manufacturer data (Apple, Samsung, Google, Microsoft).

class Scanner: NSObject, CBCentralManagerDelegate {
    var mgr: CBCentralManager!
    var throttle: [UUID: TimeInterval] = [:]
    var scanStart: TimeInterval = 0
    let scanCycleInterval: TimeInterval = 1800  // restart scan every 30min to free CB memory

    override init() {
        super.init()
        mgr = CBCentralManager(delegate: self, queue: nil)
    }

    func startScan() {
        mgr.scanForPeripherals(withServices: nil, options: [
            CBCentralManagerScanOptionAllowDuplicatesKey: true
        ])
        scanStart = Date().timeIntervalSince1970
        throttle.removeAll()  // clear stale throttle entries on restart
    }

    func centralManagerDidUpdateState(_ central: CBCentralManager) {
        if central.state == .poweredOn {
            startScan()
            emit(["type": "status", "state": "scanning",
                  "ts": Date().timeIntervalSince1970])
        } else {
            let msg: String
            switch central.state {
            case .poweredOff: msg = "Bluetooth is powered off"
            case .unauthorized: msg = "Bluetooth permission denied"
            case .unsupported: msg = "Bluetooth not supported"
            default: msg = "Bluetooth state: \(central.state.rawValue)"
            }
            emit(["type": "error", "message": msg,
                  "ts": Date().timeIntervalSince1970])
        }
    }

    func centralManager(_ central: CBCentralManager,
                         didDiscover peripheral: CBPeripheral,
                         advertisementData: [String: Any],
                         rssi RSSI: NSNumber) {
        let rssi = RSSI.intValue
        // Skip invalid RSSI
        if rssi == 127 || rssi < -100 { return }

        let uuid = peripheral.identifier
        let now = Date().timeIntervalSince1970

        // Throttle: max 1 output per UUID per second
        if let last = throttle[uuid], now - last < 1.0 { return }
        throttle[uuid] = now

        // Prune throttle cache
        if throttle.count > 200 {
            throttle = throttle.filter { now - $0.value < 10 }
        }

        // Periodic scan restart to free CoreBluetooth peripheral refs
        if now - scanStart > scanCycleInterval {
            central.stopScan()
            startScan()
            emit(["type": "status", "state": "scan_restarted",
                  "ts": now])
        }

        var obj: [String: Any] = [
            "type": "ble",
            "uuid": uuid.uuidString,
            "rssi": rssi,
            "ts": now,
        ]

        // Device name
        let name = peripheral.name
            ?? advertisementData[CBAdvertisementDataLocalNameKey] as? String
        if let name = name { obj["name"] = name }

        // Vendor + device class from manufacturer data
        if let mfg = advertisementData[CBAdvertisementDataManufacturerDataKey] as? Data,
           mfg.count >= 2 {
            let cid = UInt16(mfg[0]) | (UInt16(mfg[1]) << 8)
            switch cid {
            case 76:  obj["vendor"] = "Apple"
            case 6:   obj["vendor"] = "Microsoft"
            case 117: obj["vendor"] = "Samsung"
            case 224: obj["vendor"] = "Google"
            case 89:  obj["vendor"] = "Nordic"
            default:  break
            }

            // Apple Nearby Info: parse TLV after 2-byte company ID
            if cid == 76 {
                var i = 2
                while i + 1 < mfg.count {
                    let subType = mfg[i]
                    let subLen = Int(mfg[i + 1])
                    if subType == 0x10 && subLen >= 1 && i + 2 < mfg.count {
                        let devType = mfg[i + 2] & 0x0F
                        switch devType {
                        case 1: obj["class"] = "phone"    // iPhone
                        case 2: obj["class"] = "tablet"   // iPad
                        case 3: obj["class"] = "laptop"   // MacBook
                        case 4: obj["class"] = "watch"    // Apple Watch
                        case 5: obj["class"] = "desktop"  // iMac/Mac
                        case 6: obj["class"] = "earbuds"  // AirPods
                        case 7: obj["class"] = "tv"       // Apple TV
                        default: break
                        }
                        break
                    }
                    i += 2 + subLen
                    if subLen == 0 { break }  // avoid infinite loop
                }
            }
        }

        // Name-based device class fallback (non-Apple or when Nearby Info unavailable)
        if obj["class"] == nil, let n = name?.lowercased() {
            if n.contains("iphone") || n.contains("galaxy") || n.contains("pixel")
                || n.contains("phone") || n.contains("sm-") || n.contains("moto")
                || n.contains("oneplus") || n.contains("redmi") || n.contains("poco") {
                obj["class"] = "phone"
            } else if n.contains("macbook") || n.contains("laptop") || n.contains("thinkpad")
                || n.contains("surface") || n.contains("dell") || n.contains("hp ") {
                obj["class"] = "laptop"
            } else if n.contains("ipad") || n.contains("tab") {
                obj["class"] = "tablet"
            } else if n.contains("watch") || n.contains("band") || n.contains("fit") {
                obj["class"] = "watch"
            } else if n.contains("pods") || n.contains("buds") || n.contains("airpod")
                || n.contains("earbuds") || n.contains("headphone") {
                obj["class"] = "earbuds"
            }
        }

        // TX power for distance estimation
        if let tx = advertisementData[CBAdvertisementDataTxPowerLevelKey] as? NSNumber {
            obj["tx"] = tx.intValue
        }

        // Connectable flag hints at device type
        if let conn = advertisementData[CBAdvertisementDataIsConnectable] as? NSNumber {
            obj["connectable"] = conn.boolValue
        }

        emit(obj)
    }

    func emit(_ obj: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: obj),
           let str = String(data: data, encoding: .utf8) {
            print(str)
        }
    }
}

setbuf(stdout, nil)
let scanner = Scanner()
RunLoop.current.run()
