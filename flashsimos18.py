import argparse
import isotp
import time
import udsoncan
from udsoncan.connections import IsoTPSocketConnection
from udsoncan.client import Client
from udsoncan import configs
from udsoncan import exceptions
from udsoncan import services
from sa2_seed_key.sa2_seed_key import Sa2SeedKey

parser = argparse.ArgumentParser(description='Flash Simos18 ECU.', epilog="For example, --block 1 --file cboot.compressed.encrypted --block 2 asw1.compressed.encrypted")
parser.add_argument('--block', type=int, action="append",
                    help='which blocks to flash, numerically. blocks numbered greater than 5 will be flashed as patches to (blocknum - 5)',
                    required=True)
parser.add_argument('--file', type=str, action="append",
                    help='which blocks to flash, numerically',
                    required=True)
parser.add_argument('--tunertag', type=str, default="",
                    help='(optional) tuner tag for 3E manual checksum bypass')

args = parser.parse_args()

# We rely on dict retaining insertion order which is part of the language as of 3.7
block_files = dict(zip(args.block, args.file))

tuner_tag = args.tunertag

class DataRecord:
  address: int
  parse_type: int
  description:  str
  def __init__(self, address, parse_type, description):
    self.address = address
    self.parse_type = parse_type
    self.description = description

class GenericStringCodec(udsoncan.DidCodec):
  def encode(self, val):
    return bytes(val)

  def decode(self, payload):
    return str(payload, "ascii")

  def __len__(self):
    raise udsoncan.DidCodec.ReadAllRemainingData

class GenericBytesCodec(udsoncan.DidCodec):
  def encode(self, val):
    return bytes(val)

  def decode(self, payload):
    return payload.hex()

  def __len__(self):
    raise udsoncan.DidCodec.ReadAllRemainingData

data_records = [
  DataRecord(0xF190, 0, "VIN Vehicle Identification Number"),
  DataRecord(0xF19E, 0, "ASAM/ODX File Identifier"),
  DataRecord(0xF1A2, 0, "ASAM/ODX File Version"),
  DataRecord(0xF40D, 1, "Vehicle Speed"),
  DataRecord(0xF806, 1, "Calibration Verification Numbers"),
  DataRecord(0xF187, 0, "VW Spare Part Number"),
  DataRecord(0xF189, 0, "VW Application Software Version Number"),
  DataRecord(0xF191, 0, "VW ECU Hardware Number"),
  DataRecord(0xF1A3, 0, "VW ECU Hardware Version Number"),
  DataRecord(0xF197,0,"VW System Name Or Engine Type"),
  DataRecord(0xF1AD,0,"Engine Code Letters"),
  DataRecord(0xF1AA,0,"VW Workshop System Name"),
  DataRecord(0x0405,1,"State Of Flash Memory"),
  DataRecord(0x0407,1,"VW Logical Software Block Counter Of Programming Attempts"),
  DataRecord(0x0408,1,"VW Logical Software Block Counter Of Successful Programming Attempts"),
  DataRecord(0x0600,1,"VW Coding Value"),
  DataRecord(0xF186,1,"Active Diagnostic Session"),
  DataRecord(0xF18C,0,"ECU Serial Number"),
  DataRecord(0xF17C,0,"VW FAZIT Identification String"),
  DataRecord(0xF442,1,"Control Module Voltage"),
  DataRecord(0xEF90,1,"Immobilizer Status SHE"),
  DataRecord(0xF1F4,0,"Boot Loader Identification"),
  DataRecord(0xF1DF,1,"ECU Programming Information"),
  DataRecord(0xF1F1,1,"Tuning Protection SO2"),
  DataRecord(0xF1E0,1,""),
  DataRecord(0x12FC,1,""),
  DataRecord(0x12FF,1,""),
  DataRecord(0xFD52,1,""),
  DataRecord(0xFD83,1,""),
  DataRecord(0xFDFA,1,""),
  DataRecord(0xFDFC,1,""),
  DataRecord(0x295A,1,"Vehicle Mileage"),
  DataRecord(0x295B,1,"Control Module Mileage"),
  DataRecord(0xF190,0,"VIN Vehicle Identification Number"),
  DataRecord(0xF19E,0,"ASAM/ODX File Identifier"),
  DataRecord(0xF1A2,0,"ASAM/ODX File Version"),
  DataRecord(0xF15B,1,"Fingerprint and Programming Date"),
  DataRecord(0xF191,0,"VW ECU Hardware Number"),
  DataRecord(0xF1A3,0,"VW ECU Hardware Version Number"),
  DataRecord(0xF187,0,"VW Spare Part Number"),
  DataRecord(0xF189,0,"VW Application Software Version Number"),
  DataRecord(0xF1F4,0,"Boot Loader Identification"),
  DataRecord(0xF197,0,"VW System Name Or Engine Type"),
  DataRecord(0xF1AD,0,"Engine Code Letters"),
  DataRecord(0xF17C,0,"VW FAZIT Identification String"),
  DataRecord(0xF1A5,1,"VW Coding Repair Shop Code Or Serial Number (Coding Fingerprint),"),
  DataRecord(0x0405,1,"State Of Flash Memory"),
  DataRecord(0xF1AB,0,"VW Logical Software Block Version"),
  DataRecord(0xF804,0,"Calibration ID"),
  DataRecord(0xF17E,0,"ECU Production Change Number")
]

def volkswagen_security_algo(level, seed, params=None):
  simos18_sa2_script = bytearray([0x68, 0x02, 0x81, 0x4A, 0x10, 0x68, 0x04, 0x93, 0x08, 0x08, 0x20, 0x09, 0x4A, 0x05, 0x87, 0x22, 0x12, 0x19, 0x54, 0x82, 0x49, 0x93, 0x07, 0x12, 0x20, 0x11, 0x82, 0x4A, 0x05, 0x87, 0x03, 0x11, 0x20, 0x10, 0x82, 0x4A, 0x01, 0x81, 0x49, 0x4C])
  vs = Sa2SeedKey(simos18_sa2_script, int.from_bytes(seed, "big"))
  return vs.execute().to_bytes(4, 'big')

block_lengths = {
  1: 0x23e00,
  2: 0xffc00,
  3: 0xbfc00,
  4: 0x7fc00,
  5: 0x7fc00
}
block_transfer_sizes = {
  1: 0xFFD,
  2: 0xFFD,
  3: 0xFFD,
  4: 0xFFD,
  5: 0xFFD
}

# When we're performing WriteWithoutErase, we need to write very slowly to allow the un-erased flash to soak - but when we're just "writing" 0s (which we can't actually do), we can go faster.
# Internally, we're basically stuffing the mailbox for a flash task, so we also need to repeat many requests as the mailbox will be full but the prior transfer request does not wait for readiness to return. 
def block_transfer_sizes_patch(block_number, address):
  if(block_number != 4):
    print("Only patching Block 4 / ASW3 is supported at this time!")
    exit()
  if(address < 0x95FF):
    return 0x100
  if(address >= 0x95FF and address < 0x9800):
    return 0x8
  if(address >= 0x9800 and address < 0x7DD00):
    return 0x100
  if(address >= 0x7DD00 and address < 0x7E200):
    return 0x8
  if(address >= 0x7E200):
    return 0x100
  return 0x8

udsoncan.setup_logging()

params = {
  'tx_padding': 0x55
}

def send_obd(data):
  conn2 = IsoTPSocketConnection('can0', rxid=0x7E8, txid=0x700, params=params)
  conn2.tpsock.set_opts(txpad=0x55, tx_stmin=2500000)
  conn2.open()
  conn2.send(data)
  conn2.wait_frame()
  conn2.wait_frame()
  conn2.close()

print("Sending 0x4 Clear Emissions DTCs over OBD-2")
send_obd(bytes([0x4]))

conn = IsoTPSocketConnection('can0', rxid=0x7E8, txid=0x7E0, params=params)
conn.tpsock.set_opts(txpad=0x55, tx_stmin=2500000)
with Client(conn, request_timeout=5, config=configs.default_client_config) as client:
   try:
      client.config['security_algo'] = volkswagen_security_algo

      client.config['data_identifiers'] = {}
      for data_record in data_records:
        if(data_record.parse_type == 0):
          client.config['data_identifiers'][data_record.address] = GenericStringCodec
        else:
          client.config['data_identifiers'][data_record.address] = GenericBytesCodec

      client.config['data_identifiers'][0xF15A] = GenericBytesCodec

      print("Opening extended diagnostic session...")
      client.change_session(services.DiagnosticSessionControl.Session.extendedDiagnosticSession)

      print("Reading ECU information...")
      for i in range(33, 47):
        did = data_records[i]
        response = client.read_data_by_identifier_first(did.address)
        print(did.description + " : " + response)

      # Check Programming Precondition (load CBOOT)
      print("Checking programming precondition, routine 0x0203...")
      client.start_routine(0x0203)

      client.tester_present()

      # Upgrade to Programming Session
      print("Upgrading to programming session...")
      client.change_session(services.DiagnosticSessionControl.Session.programmingSession)

      # Fix timeouts to work around overly smart library
      client.session_timing['p2_server_max'] = 30
      client.config['request_timeout'] = 30

      client.tester_present()

      # Perform Seed/Key Security Level 17
      print("Performing Seed/Key authentication...")
      client.unlock_security_access(17)

      client.tester_present()

      print("Writing flash tool log to LocalIdentifier 0xF15A...")
      # Write Flash Tool Workshop Log (TODO real/fake date/time)
      client.write_data_by_identifier(0xF15A, bytes([
          0x14,
          0x7,
          0x17,
          0x0,
          0x7,
          0xe6,
          0x2c,
          0x0,
          0x42
      ]))

      client.tester_present()

      def next_counter(counter):
        if(counter == 0xFF):
          return 0
        else:
          return (counter + 1)

      def flash_block(client, block_files, block_number):
        data = open(block_files[block_number], "rb").read()

        print("Erasing block " + str(block_number) + ", routine 0xFF00...")
        # Erase Flash
        client.start_routine(0xFF00, data=bytes([0x1, block_number]))

        print("Requesting download for block " + str(block_number) + " of length " + str(block_lengths[block_number]) + " ...")
        # Request Download
        dfi = udsoncan.DataFormatIdentifier(compression=0xA, encryption=0xA)
        memloc = udsoncan.MemoryLocation(block_number, block_lengths[block_number])
        client.request_download(memloc, dfi=dfi)

        print("Transferring data... " + str(len(data)) + " bytes to write")
        # Transfer Data
        counter = 1
        for block_base_address in range(0, len(data), block_transfer_sizes[block_number]):
          print("Transferring " + str(block_base_address) + " of " + str(len(data)) + " bytes")
          block_end = min(len(data), block_base_address+block_transfer_sizes[block_number])
          client.transfer_data(counter, data[block_base_address:block_end])
          counter = next_counter(counter)

        print("Exiting transfer...")
        # Exit Transfer
        client.request_transfer_exit()

        if((len(tuner_tag) > 0) and (block_number > 1)):
          print("Sending tuner ASW magic number...")
          # Send Magic
          # In the case of a tuned ASW, send 6 tune-specific magic bytes after this 3E to force-overwrite the CAL validity area
          def tuner_payload(payload, tune_block_number=block_number):
            return payload + bytes(tuner_tag, "ascii") + bytes([tune_block_number])

          with client.payload_override(tuner_payload):
            client.tester_present()
        else:
          client.tester_present()

        print("Checksumming block " + str(block_number) + " , routine 0x0202...")
        # Checksum
        client.start_routine(0x0202, data=bytes([0x01, block_number, 0, 0x04, 0, 0, 0, 0]))

      # patch_block takes a block index and subtracts 5 to pick the block to actually patch.
      # for example [1: file1, 2: file2, 3: file3, 4: file4, 9: file4_patch, 5: file5]
      def patch_block(client, block_files, block_number):
        data = open(block_files[block_number], "rb").read()
        block_number = block_number - 5

        print("Erasing block " + str(block_number + 1) + ", routine 0xFF00...")
        # Erase Flash
        client.start_routine(0xFF00, data=bytes([0x1, block_number + 1]))

        print("Requesting download for PATCH block " + str(block_number) + " of length " + str(block_lengths[block_number]) + " ...")
        # Request Download
        dfi = udsoncan.DataFormatIdentifier(compression=0x0, encryption=0xA)
        memloc = udsoncan.MemoryLocation(block_number, block_lengths[block_number], memorysize_format=32)
        client.request_download(memloc, dfi=dfi)

        print("Transferring PATCH data... " + str(len(data)) + " bytes to write")

        # Transfer Data
        counter = 1
        transfer_address = 0
        while(transfer_address < len(data)):
          transfer_size = block_transfer_sizes_patch(block_number, transfer_address)
          print("Transferring PATCH:" + str(transfer_address) + " of " + str(len(data)) + " bytes using size " + str(transfer_size))
          block_end = min(len(data), transfer_address+transfer_size)
          transfer_data = data[transfer_address:block_end]

          success = False

          while(success == False):
            try:
              client.transfer_data(counter, transfer_data)
              success = True
              counter = next_counter(counter)
            except exceptions.NegativeResponseException as e:
              print('PATCH refused block (EXPECTED): %s with code "%s" (0x%02x)' % (e.response.service.get_name(), e.response.code_name, e.response.code))
              success = False
              counter = next_counter(counter)

          transfer_address += transfer_size

        print("Exiting PATCH transfer...")
        # Exit Transfer
        client.request_transfer_exit()

      for block in block_files:
        if block <= 5:
          flash_block(client, block_files, block)
        if block > 5:
          patch_block(client, block_files, block)

      print("Verifying programming dependencies, routine 0xFF01...")
      # Verify Programming Dependencies
      client.start_routine(0xFF01)

      client.tester_present()

      # If a periodic task was patched or altered as part of the process, let's give it a few seconds to run
      time.sleep(5)

      print("Rebooting ECU...")
      # Reboot
      client.ecu_reset(services.ECUReset.ResetType.hardReset)

      print("Sending 0x4 Clear Emissions DTCs over OBD-2")
      send_obd(bytes([0x4]))

      print("Clearing DTC...")
      client.change_session(services.DiagnosticSessionControl.Session.extendedDiagnosticSession)
      client.tester_present()
      client.control_dtc_setting(udsoncan.services.ControlDTCSetting.SettingType.off, data=bytes([0xFF, 0xFF, 0xFF]))

      print("Done!")
   except exceptions.NegativeResponseException as e:
      print('Server refused our request for service %s with code "%s" (0x%02x)' % (e.response.service.get_name(), e.response.code_name, e.response.code))
   except exceptions.InvalidResponseException as e:
      print('Server sent an invalid payload : %s' % e.response.original_payload)
   except exceptions.UnexpectedResponseException as e:
      print('Server sent an invalid payload : %s' % e.response.original_payload)
